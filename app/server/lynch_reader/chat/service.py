"""Chats: one Claude Code or opencode process per active conversation, or one grok process per discover turn; their state, and fan-out to the app's event streams.

Everything here runs on the server's event loop; nothing blocks it except small file reads.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lynch_reader.chat import transcript
from lynch_reader.chat.approvals import (AUTO_DENY_MESSAGE, USER_DENY_MESSAGE, Approval, approvable_path,
                                         new_approval)
from lynch_reader.chat.candidates import CandidateStore
from lynch_reader.chat.watch import WatchStore
from lynch_reader.chat.config import (CHAT_EFFORTS, CHAT_MODELS, DEFAULT_EFFORT, DEFAULT_MODEL, IDLE_TIMEOUT_S,
                                      MAX_PROCESSES, LaunchConfig, launch_argv, launch_env)
from lynch_reader.chat.grok import (DEFAULT_GROK_MODEL, DEFAULT_WINDOW, MAX_TURNS, GrokConfig, GrokSetupError,
                                    discover_prompt, grok_argv, grok_env, grok_models, new_session, prepare_workdir)
from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.index import ChatIndex, Conversation, make_title
from lynch_reader.chat.items import (compact_item, context_tokens, framework_ticker, message_items, notice_item,
                                     retry_text)
from lynch_reader.chat.kol import (DEFAULT_LOOKBACK_DAYS, LOOKBACK_DAYS, KolSources, KolUnavailable, kol_brief,
                                   kol_sources)
from lynch_reader.chat.opencode import (DEFAULT_OPENCODE_MODEL, OPENCODE_EFFORT, OPENCODE_MODELS, OpencodeConfig,
                                        opencode_env, write_rules)
from lynch_reader.chat.opencode_link import LinkEvents, OpencodeLink, OpencodeSetupError
from lynch_reader.chat.process import ChatProcess

log = logging.getLogger(__name__)

BUSY = ("running", "compacting")
COMPACT_COMMAND = "/compact"
KINDS = ("research", "ticker", "discover")
AGENT_LABELS = {"claude": "Claude Code", "opencode": "opencode", "grok": "Grok"}


def _process_name(agent: str) -> str:
    """How notices about a chat's process name it; Claude's predate the agent labels."""
    return "Claude" if agent == "claude" else AGENT_LABELS[agent]


def _grok_failure(result: dict) -> str | None:
    """What to tell the user when grok's `result` line is an error subtype; None for a successful turn."""
    if result.get("subtype") == "success" and result.get("is_error") not in (True, "True"):
        return None
    if result.get("subtype") == "error_max_turns":
        return (f"Grok 达到单轮步数上限（{MAX_TURNS} 步），本轮提前结束。"
                "可以缩小搜索范围再试：少看几位 KOL，或缩短回看时长。")
    errors = result.get("errors")
    detail = "；".join(map(str, errors)) if isinstance(errors, list) else str(result.get("result") or "")
    detail = detail.strip()[:300]
    return f"Grok 本轮出错结束：{detail}" if detail else "Grok 本轮出错结束"


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Invalid(Exception):
    pass


@dataclass
class Live:
    """Runtime state of one conversation. No entry means dormant with nobody watching."""
    state: str = "dormant"  # running | compacting | idle | dormant
    proc: ChatProcess | OpencodeLink | None = None
    last_used: float = 0.0
    partial: str = ""  # streamed text not yet closed by its full message
    retry: str | None = None  # the API request being retried, until the model answers or the turn ends
    context_tokens: int | None = None
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    # A new conversation's first message, until its first reply ends and claude is asked for a title;
    # then the id of that request, whose answer replaces the title unless the user renamed meanwhile.
    title_from: str | None = None
    title_request: str | None = None
    # (resume uuid, transcript entry count) of an edit whose new process has not written yet;
    # until it does, the file's newest message is still the edited-away branch.
    pending_cut: tuple[str, int] | None = None
    # Discover only: this turn's grok printed its `result`, so its exit is the normal end of the turn.
    result_seen: bool = False
    # Skill edits the agent is waiting on the phone to allow or deny, by request id, in arrival order.
    # They belong to `proc`: whatever ends it (exit, stop, edit, archive) drops them.
    approvals: dict[str, Approval] = field(default_factory=dict)
    # Held by public commands that spawn or stop this chat's own process (send, edit, archive),
    # so two of them never race on `proc`. `_spawn` / `_stop` / `_make_room` never take it:
    # `_make_room` stops other chats' idle processes without their lock, so no lock-order deadlock.
    # Eviction (`_make_room`, `reap_idle`) skips a chat whose lock is held: a command is writing to it.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ChatService:
    def __init__(self, index: ChatIndex, launch: LaunchConfig, projects_dir: Path, base_env: Mapping[str, str], *,
                 grok: GrokConfig | None = None, candidates: CandidateStore | None = None,
                 watch: WatchStore | None = None, opencode: OpencodeConfig | None = None,
                 monotonic: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 idle_timeout_s: float = IDLE_TIMEOUT_S, max_processes: int = MAX_PROCESSES) -> None:
        self._index = index
        self._launch = launch
        self._projects = projects_dir
        self._env = launch_env(base_env)
        self._grok = grok
        self._grok_env = grok_env(base_env)
        self._grok_logs: dict[str, AppLog] = {}
        self._opencode = opencode
        self._opencode_env = dict(base_env)  # opencode bills its own providers; the Claude billing guard is moot
        self.candidates = candidates
        self.watch = watch
        self._now, self._wall = monotonic, wall
        self._idle_timeout_s, self._max_processes = idle_timeout_s, max_processes
        self._live: dict[str, Live] = {}
        self._tasks: set[asyncio.Task] = set()
        self.usage: dict | None = None

    # ---- queries

    def options(self) -> dict:
        models = grok_models(self._grok.home) if self._grok else []
        default = next((m for m in models if m.id == DEFAULT_GROK_MODEL), models[0] if models else None)
        discover = {"models": [m.public() for m in models], "default_model": default.id if default else None,
                    "default_window": (DEFAULT_WINDOW if default is None or DEFAULT_WINDOW in default.windows
                                       else default.windows[0]),
                    "lookback_days": list(LOOKBACK_DAYS), "default_lookback_days": DEFAULT_LOOKBACK_DAYS,
                    "hint": None if models else "在终端运行 grok models 刷新 Grok 模型清单"}
        # The top-level models and efforts are Claude Code's, as before agents; a new 研究 / 讨论 chat starts on
        # Claude Code whatever the last one used (user, 2026-10-18).
        return {"models": [{"id": m, "label": label} for m, label in CHAT_MODELS], "efforts": list(CHAT_EFFORTS),
                "default_model": DEFAULT_MODEL, "default_effort": DEFAULT_EFFORT, "discover": discover,
                "agents": [{"id": a, "label": AGENT_LABELS[a]} for a in ("claude", "opencode")],
                "default_agent": "claude",
                "opencode": {"models": [{"id": m, "label": label} for m, label in OPENCODE_MODELS],
                             "default_model": DEFAULT_OPENCODE_MODEL}}

    def list(self) -> dict:
        rows = [self._summary(c) for c in self._index.all() if self._visible(c)]
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        return {"chats": rows, "running_count": sum(r["state"] in BUSY for r in rows), "usage": self.usage}

    def ticker_chat_starts(self) -> list[tuple[str, float]]:
        """(ticker, created_at) of every 讨论 chat, archived ones included: the earnings list counts any chat on a
        ticker started after its release as research on it."""
        return [(c.ticker, c.created_at) for c in self._index.all() if c.kind == "ticker" and c.ticker]

    def snapshot(self, conv_id: str) -> dict:
        conv = self._conv(conv_id)
        live = self._live.get(conv_id) or Live()
        path = self._transcript(conv_id)
        entries = transcript.read_entries(path) if path.exists() else []
        tip = None
        if live.pending_cut is not None:
            cut, count = live.pending_cut
            if transcript.has_message_after(entries, count):
                live.pending_cut = None
            else:
                tip = cut
        # All entries, not just the branch: parallel tool results hang off the branch (Task 4 fix).
        hist = transcript.history(transcript.current_branch(entries, tip), entries)
        items = list(hist.items)
        if hist.unfinished and live.state == "dormant":
            items.append({"kind": "notice", "key": "interrupted", "text": "上次回答被中断"})
        tokens = live.context_tokens if live.context_tokens is not None else hist.context_tokens
        return {"conv": self._summary(conv), "items": items, "partial": live.partial, "retry": live.retry,
                "usage": self.usage if conv.agent == "claude" else None,  # Claude's subscription, nobody else's
                "context": {"tokens": tokens, "window": conv.context_window},
                "approvals": [a.public() for a in live.approvals.values()]}

    # ---- commands

    async def create(self, kind: str, ticker: str | None, text: str, model: str, effort: str,
                     context_window: object = None, lookback_days: object = None,
                     agent: str | None = None) -> Conversation:
        # The values come from a JSON body; a list or dict would break the membership tests below.
        if (not all(isinstance(v, str) for v in (kind, text, model, effort))
                or not all(isinstance(v, str | None) for v in (ticker, agent))):
            raise Invalid("fields must be strings")
        if kind not in KINDS:
            raise Invalid(f"unknown kind {kind!r}")
        if not text.strip():
            raise Invalid("empty message")
        # 发现新票 runs only on Grok; 研究 / 讨论 on Claude Code or opencode (Claude Code when unnamed).
        agent = agent or ("grok" if kind == "discover" else "claude")
        if agent not in (("grok",) if kind == "discover" else ("claude", "opencode")):
            raise Invalid(f"agent {agent!r} cannot run a {kind} chat")
        if agent == "grok":
            conv = await self._new_discover(text, model, effort, context_window, lookback_days)
        elif agent == "opencode":
            conv = self._new_opencode(kind, ticker, text, model, effort)
        else:
            conv = self._new_claude(kind, ticker, text, model, effort)
        self._index.put(conv)
        # Visible in the list before its transcript exists. Claude titles a chat after its first reply; the others
        # keep the first message.
        self._live[conv.id] = Live(title_from=text if agent == "claude" else None)
        await self.send(conv.id, text)
        return conv

    def _new_claude(self, kind: str, ticker: str | None, text: str, model: str, effort: str) -> Conversation:
        if kind == "ticker" and not ticker:
            raise Invalid("a ticker conversation needs a ticker")
        if model not in {m for m, _ in CHAT_MODELS}:
            raise Invalid(f"unknown model {model!r}")
        if effort not in CHAT_EFFORTS:
            raise Invalid(f"unknown effort {effort!r}")
        now = self._wall()
        # The window arrives with the first turn's result; until then, the last one seen for this model.
        seen = [c for c in self._index.all() if c.model == model and c.context_window]
        window = max(seen, key=lambda c: c.updated_at).context_window if seen else None
        return Conversation(id=str(uuid.uuid4()), kind=kind, ticker=ticker if kind == "ticker" else None,
                            title=make_title(text), model=model, effort=effort, created_at=now, updated_at=now,
                            context_window=window)

    def _new_opencode(self, kind: str, ticker: str | None, text: str, model: str, effort: str) -> Conversation:
        if self._opencode is None:
            raise Invalid("opencode 未配置")
        if kind == "ticker" and not ticker:
            raise Invalid("a ticker conversation needs a ticker")
        if model not in {m for m, _ in OPENCODE_MODELS}:
            raise Invalid(f"unknown opencode model {model!r}")
        if effort != OPENCODE_EFFORT:
            raise Invalid(f"unknown effort {effort!r}")
        now = self._wall()
        # The window arrives with the first turn's usage.
        return Conversation(id=str(uuid.uuid4()), kind=kind, ticker=ticker if kind == "ticker" else None,
                            title=make_title(text), model=model, effort=effort, created_at=now, updated_at=now,
                            agent="opencode")

    async def _new_discover(self, text: str, model: str, effort: str, window: object, days: object) -> Conversation:
        grok = self._require_grok()
        chosen = next((m for m in grok_models(grok.home) if m.id == model), None)
        if chosen is None:
            raise Invalid(f"unknown Grok model {model!r}")
        if effort not in chosen.efforts:
            raise Invalid(f"unknown effort {effort!r}")
        # From JSON: 256000.0 equals 256000 and True equals 1, but neither is a window grok takes.
        if type(window) is not int or window not in chosen.windows:
            raise Invalid(f"unknown context window {window!r}")
        days = DEFAULT_LOOKBACK_DAYS if days is None else days
        if type(days) is not int or days not in LOOKBACK_DAYS:
            raise Invalid(f"unknown lookback {days!r}")
        self._discover_skill()  # no session for a chat that could not run
        await self._kol_sources()  # likewise
        try:
            session = await new_session(grok, self._grok_env, model, window)
        except (GrokSetupError, OSError) as e:
            raise Conflict(f"无法创建 Grok 会话：{e}") from e
        now = self._wall()
        return Conversation(id=str(uuid.uuid4()), kind="discover", ticker=None, title=make_title(text), model=model,
                            effort=effort, created_at=now, updated_at=now, context_window=window,
                            grok_session=session, lookback_days=days, agent="grok")

    async def send(self, conv_id: str, text: str) -> None:
        conv = self._conv(conv_id)
        if not text.strip():
            raise Invalid("empty message")
        live = self._live.setdefault(conv_id, Live())
        async with live.lock:
            self._check_not_archived(conv_id, live)
            if conv.agent == "grok":
                await self._start_grok_turn(conv, live, text)
                return
            if conv.agent == "opencode":
                await self._send_opencode(conv, live, text)
                return
            if text.lstrip().startswith(COMPACT_COMMAND) and live.state in BUSY:
                raise Conflict("等这一轮结束再压缩")
            if live.proc is None or not live.proc.alive:
                await self._spawn(conv)
            await self._write_user(conv_id, text)

    async def interrupt(self, conv_id: str) -> None:
        conv = self._conv(conv_id)
        live = self._live.get(conv_id)
        if not (live and live.proc and live.proc.alive and live.state in BUSY):
            return
        if conv.agent == "grok":
            await self._stop_grok_turn(conv_id, live)
            return
        try:
            await live.proc.interrupt()  # Claude's interrupt message, or opencode's session/cancel
        except (BrokenPipeError, ConnectionResetError) as e:  # it exited after the alive check
            raise Conflict(f"{_process_name(conv.agent)} 进程已退出，原因见对话里的提示") from e

    async def edit(self, conv_id: str, message_uuid: str, text: str) -> None:
        """Truncate the conversation before `message_uuid` and send `text` in its place. Files are not touched."""
        conv = self._conv(conv_id)
        if conv.agent != "claude":  # only Claude Code can resume a session at an earlier message
            raise Invalid("发现新票对话不支持编辑消息" if conv.agent == "grok" else
                          f"{AGENT_LABELS[conv.agent]} 对话不支持编辑消息")
        live = self._live.setdefault(conv_id, Live())
        async with live.lock:
            self._check_not_archived(conv_id, live)
            if live.state in BUSY:
                raise Conflict("回答进行中，先停止再编辑")
            if not text.strip():
                raise Invalid("empty message")
            path = self._transcript(conv_id)
            entries = transcript.read_entries(path) if path.exists() else []
            try:
                cut = transcript.edit_cut(transcript.current_branch(entries), message_uuid)
            except transcript.EditRejected as e:
                raise Invalid(str(e)) from e
            await self._stop(conv_id)
            await self._spawn(conv, resume_at=cut)
            live.pending_cut = (cut, len(entries))
            live.context_tokens = None  # the pre-edit count belongs to the dropped turns
            self._broadcast(conv_id, "reset", {})  # the branch changed: watchers refetch a snapshot
            await self._write_user(conv_id, text)

    async def archive(self, conv_id: str) -> None:
        """Hide the conversation; its process dies at once, whatever it was doing."""
        self._conv(conv_id)
        live = self._live.setdefault(conv_id, Live())
        async with live.lock:
            await self._stop(conv_id)
            self._index.update(conv_id, archived=True)
            self._broadcast(conv_id, "archived", {})
        self._live.pop(conv_id, None)

    async def archive_ticker(self, ticker: str) -> int:
        ids = [c.id for c in self._index.all() if c.ticker == ticker and self._visible(c)]
        archived = 0
        for conv_id in ids:
            try:
                await self.archive(conv_id)
            except NotFound:
                continue  # archived by someone else while we were stopping an earlier one
            archived += 1
        return archived

    def rename(self, conv_id: str, title: object) -> None:
        """The first message makes the title; the user may replace it. Leaves updated_at alone: not activity."""
        self._conv(conv_id)
        if not isinstance(title, str) or not title.strip():
            raise Invalid("empty title")
        live = self._live.get(conv_id)
        if live is not None:  # the user's title wins over claude's, even one still on its way
            live.title_from = live.title_request = None
        conv = self._index.update(conv_id, title=make_title(title))
        self._broadcast(conv_id, "conv", self._summary(conv))

    async def answer_approval(self, conv_id: str, request_id: str, allow: object) -> None:
        """The phone's answer to one pending skill edit."""
        conv = self._conv(conv_id)
        if not isinstance(allow, bool):
            raise Invalid("allow must be true or false")
        live = self._live.get(conv_id)
        # Popped before the write, so a double tap cannot answer claude twice.
        approval = live.approvals.pop(request_id, None) if live else None
        if approval is None or live.proc is None:
            raise Conflict("这处改动已不在等待审批")
        self._broadcast(conv_id, "approval_done", {"request_id": request_id})
        try:
            if conv.agent == "opencode":
                await live.proc.answer(request_id, allow)
            else:
                await live.proc.respond(request_id, {"behavior": "allow", "updatedInput": approval.tool_input} if allow
                                        else {"behavior": "deny", "message": USER_DENY_MESSAGE})
        except (BrokenPipeError, ConnectionResetError) as e:
            raise Conflict(f"{_process_name(conv.agent)} 进程已退出，原因见对话里的提示") from e

    def save_candidates(self, conv_id: str, key: object, tickers: object) -> list[str]:
        """The user's 存入候选区: only candidates Grok's own reply carries, looked up again in the transcript."""
        if self._conv(conv_id).kind != "discover":
            raise Invalid("只有发现新票对话能存入候选区")
        if (not isinstance(key, str) or not isinstance(tickers, list) or not tickers
                or not all(isinstance(t, str) for t in tickers)):
            raise Invalid("key and a list of tickers required")
        if self.candidates is None:
            raise Conflict("候选区未配置")
        item = self._reply_item(conv_id, key, "candidates")
        if item is None:
            raise Conflict("这组候选已不在对话记录里")
        picked = [c for c in item["candidates"] if c["ticker"] in set(tickers)]
        if not picked:
            raise Conflict("所选的票不在这组候选里")
        return self.candidates.save(conv_id, picked)

    def save_watch(self, conv_id: str, key: object) -> None:
        """The user's 加入观察区: only the card the agent's own reply carries, looked up again in the transcript."""
        conv = self._conv(conv_id)
        if conv.kind != "ticker":
            raise Invalid("只有讨论已有框架的对话能加入观察区")
        if not isinstance(key, str):
            raise Invalid("key required")
        if self.watch is None:
            raise Conflict("观察区未配置")
        item = self._reply_item(conv_id, key, "watch")
        if item is None:
            raise Conflict("这张卡片已不在对话记录里")
        if item["watch"]["ticker"] != conv.ticker:
            raise Conflict(f"卡片里的票不是本次对话的 {conv.ticker}")
        self.watch.save(conv_id, item["watch"])

    def watch_list(self) -> list[dict]:
        return self.watch.snapshot(self.ticker_chat_starts()) if self.watch is not None else []

    def _reply_item(self, conv_id: str, key: str, field: str) -> dict | None:
        """The reply item `key` on the chat's current branch, if it carries `field` (a block the app parsed)."""
        path = self._transcript(conv_id)
        entries = transcript.read_entries(path) if path.exists() else []
        items = transcript.history(transcript.current_branch(entries), entries).items
        return next((i for i in items if i["key"] == key and i.get(field)), None)

    async def reap_idle(self) -> None:
        now = self._now()
        for conv_id, live in list(self._live.items()):
            if (live.state == "idle" and live.proc and not live.lock.locked()
                    and now - live.last_used >= self._idle_timeout_s):
                await self._stop(conv_id)

    def subscribe(self, conv_id: str) -> asyncio.Queue:
        self._conv(conv_id)
        q: asyncio.Queue = asyncio.Queue()
        self._live.setdefault(conv_id, Live()).subscribers.add(q)
        return q

    def unsubscribe(self, conv_id: str, q: asyncio.Queue) -> None:
        live = self._live.get(conv_id)
        if live:
            live.subscribers.discard(q)

    async def shutdown(self) -> None:
        for conv_id in list(self._live):
            await self._stop(conv_id)

    # ---- process lifecycle

    async def _spawn(self, conv: Conversation, resume_at: str | None = None) -> None:
        await self._make_room()
        live = self._live.setdefault(conv.id, Live())
        resume = self._transcript(conv.id).exists()
        argv = launch_argv(self._launch, conv, resume=resume, resume_at=resume_at)
        proc = ChatProcess(argv, self._env, self._launch.repo_root,
                           on_message=lambda msg: self._on_message(conv.id, proc, msg),
                           on_exit=lambda p, expected, err: self._on_exit(conv.id, p, expected, err))
        try:
            await proc.start()
        except OSError as e:
            raise Conflict(f"无法启动 Claude：{e}") from e
        # A dead predecessor's exit may not be reported yet, and once replaced it is ignored as stale:
        # its cards go now, or a tap would send its request id to this process.
        self._clear_approvals(conv.id, live)
        live.proc = proc
        live.partial = ""
        live.last_used = self._now()
        self._set_state(conv.id, "idle")

    async def _write_user(self, conv_id: str, text: str) -> None:
        live = self._live[conv_id]
        proc = live.proc
        if proc is None:
            raise Conflict("Claude 进程启动后立即退出，原因见对话里的提示")
        try:
            await proc.send_user(text)
        except (BrokenPipeError, ConnectionResetError) as e:
            raise Conflict("Claude 进程已退出，原因见对话里的提示") from e
        self._index.update(conv_id, updated_at=self._wall())
        if live.proc is not proc:
            return  # it exited while we wrote; its exit already set dormant and said why
        live.last_used = self._now()
        if live.state not in BUSY:
            self._set_state(conv_id, "running")

    async def _start_grok_turn(self, conv: Conversation, live: Live, text: str) -> None:
        """grok takes one message per process (`-p`): each turn is a new process resuming the conversation's
        grok session, and it exits after its `result`. Called under the chat's lock."""
        if live.state in BUSY:
            raise Conflict("回答进行中，等这一轮结束或先停止")
        if text.lstrip().startswith(COMPACT_COMMAND):
            raise Invalid("发现新票对话不支持压缩上下文")
        grok = self._require_grok()
        skill = self._discover_skill()  # both read every turn: an edit applies at once
        kol = await self._kol_sources()
        days = conv.lookback_days or DEFAULT_LOOKBACK_DAYS  # chats made before the choice existed
        prompt = discover_prompt(skill, kol_brief(kol, days, datetime.fromtimestamp(self._wall(), UTC)), text)
        await self._make_room()
        try:
            prepare_workdir(grok.workdir)
        except GrokSetupError as e:
            raise Conflict(f"无法启动 Grok：{e}") from e
        # grok never echoes the prompt: the app records and shows it itself.
        self._broadcast_entry(conv.id, self._grok_log(conv.id).user(text))
        proc = ChatProcess(grok_argv(grok, conv, prompt), self._grok_env, grok.workdir,
                           on_message=lambda msg: self._on_message(conv.id, proc, msg),
                           on_exit=lambda p, expected, err: self._on_exit(conv.id, p, expected, err))
        try:
            await proc.start()
        except OSError as e:
            raise Conflict(f"无法启动 Grok：{e}") from e
        live.proc, live.partial, live.result_seen, live.last_used = proc, "", False, self._now()
        self._index.update(conv.id, updated_at=self._wall())
        self._set_state(conv.id, "running")

    async def _stop_grok_turn(self, conv_id: str, live: Live) -> None:
        """grok has no interrupt message: the turn's process is ended, and the log marks the stop."""
        async with live.lock:
            if live.state not in BUSY:
                return  # the turn ended while we waited for the lock
            answered = live.result_seen  # grok lingering after its reply: end it, but no 已停止 after a full answer
            await self._stop(conv_id)
            if not answered:
                self._broadcast_entry(conv_id, self._grok_log(conv_id).interrupted())

    async def _send_opencode(self, conv: Conversation, live: Live, text: str) -> None:
        """One session/prompt per turn on the chat's long-lived link. Called under the chat's lock."""
        if live.state in BUSY:  # opencode takes no message mid-turn
            raise Conflict("回答进行中，等这一轮结束或先停止")
        if text.lstrip().startswith(COMPACT_COMMAND):
            raise Invalid("opencode 对话不支持压缩上下文")
        if live.proc is None or not live.proc.alive:
            await self._spawn_opencode(conv)
        link = live.proc
        try:
            await link.prompt(text)
        except (BrokenPipeError, ConnectionResetError) as e:
            raise Conflict("opencode 进程已退出，原因见对话里的提示") from e
        self._index.update(conv.id, updated_at=self._wall())
        if live.proc is link:  # else it exited while we wrote; its exit set dormant and said why
            live.last_used = self._now()
            self._set_state(conv.id, "running")

    async def _spawn_opencode(self, conv: Conversation) -> None:
        cfg = self._require_opencode()
        await self._make_room()
        live = self._live.setdefault(conv.id, Live())
        env = opencode_env(self._opencode_env, write_rules(cfg, conv))
        link = OpencodeLink(cfg, conv, env, AppLog(self._transcript(conv.id)), self._link_events(conv.id, lambda: link))
        try:
            session = await link.start()
        except OpencodeSetupError as e:
            raise Conflict(f"无法启动 opencode：{e}") from e
        if conv.opencode_session != session:
            self._index.update(conv.id, opencode_session=session)
        if link.session_lost:
            self._notice(conv.id, "opencode 找不到这个对话原来的会话，已新开一个：它不记得之前的内容")
        self._clear_approvals(conv.id, live)  # as in _spawn: a dead predecessor's cards go now
        live.proc = link
        live.partial = ""
        live.last_used = self._now()
        self._set_state(conv.id, "idle")

    def _link_events(self, conv_id: str, link: Callable[[], OpencodeLink]) -> LinkEvents:
        """The link's callbacks, each ignored once the link is no longer the chat's (stopped or replaced)."""
        def current(handler: Callable[..., None]) -> Callable[..., None]:
            def run(*args) -> None:
                live = self._live.get(conv_id)
                if live is not None and live.proc is link():
                    handler(live, *args)
            return run

        def on_delta(live: Live, text: str) -> None:
            live.partial += text
            self._broadcast(conv_id, "delta", {"text": text})

        def on_usage(live: Live, used: int | None, window: int | None, cost: float | None) -> None:
            live.context_tokens = used
            conv = self._index.get(conv_id)
            changes = {k: v for k, v in (("context_window", window), ("cost_usd", cost))
                       if v and v != getattr(conv, k)}  # cost: the session's running total
            if changes:
                conv = self._index.update(conv_id, **changes)
                self._broadcast(conv_id, "conv", self._summary(conv))
            self._broadcast(conv_id, "context", self._context(conv_id))

        def on_permission(live: Live, request_id: str, tool_use_id: str, name: str, tool_input: dict) -> None:
            request = {"tool_name": name, "input": tool_input, "tool_use_id": tool_use_id}
            approval = self._skill_approval(conv_id, request_id, request)
            if approval is None:  # anything but a skill edit is refused at once, as in Claude chats
                self._spawn_task(self._answer_link(conv_id, link(), request_id))
                return
            live.approvals[request_id] = approval
            self._broadcast(conv_id, "approval", approval.public())

        def on_turn_end(live: Live, stop: str | None, error: str | None) -> None:
            # An error reached the page already, as the turn's closing text in the record.
            self._index.update(conv_id, updated_at=self._wall())
            live.partial = ""
            live.last_used = self._now()
            self._clear_approvals(conv_id, live)
            self._set_state(conv_id, "idle")

        return LinkEvents(on_delta=current(on_delta), on_entry=current(lambda live, e: self._show(conv_id, live, e)),
                          on_usage=current(on_usage), on_permission=current(on_permission),
                          on_turn_end=current(on_turn_end),
                          on_exit=lambda expected, err: self._on_exit(conv_id, link(), expected, err))

    async def _answer_link(self, conv_id: str, link: OpencodeLink, request_id: str) -> None:
        try:
            await link.answer(request_id, False)
        except OSError as e:  # the process died: its exit already told the page
            log.info("chat %s: permission answer not sent: %s", conv_id, e)

    def _discover_skill(self) -> str:
        path = self._require_grok().skill
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            raise Conflict(f"找不到发现新票的 skill：{path}") from e

    async def _kol_sources(self) -> KolSources:
        try:
            return await kol_sources(self._require_grok().invest_root)
        except KolUnavailable as e:
            raise Conflict(f"没有可用的 KOL 名单：{e}") from e

    async def _stop(self, conv_id: str) -> None:
        live = self._live.get(conv_id)
        if live is None:
            return
        proc, live.proc = live.proc, None  # detach first: its exit is then ignored as stale
        self._clear_approvals(conv_id, live)
        if proc is not None:
            await proc.close()
        # While we waited, an archive may have dropped this chat, or a send attached a new process.
        if self._live.get(conv_id) is live and live.proc is None:
            live.partial = ""
            live.pending_cut = None  # the next process resumes from the file's newest message
            self._set_state(conv_id, "dormant")

    async def _make_room(self) -> None:
        alive = [(cid, live) for cid, live in self._live.items() if live.proc and live.proc.alive]
        excess = len(alive) + 1 - self._max_processes
        idle = sorted((live.last_used, cid) for cid, live in alive if live.state == "idle" and not live.lock.locked())
        for _, cid in idle[:max(0, excess)]:
            await self._stop(cid)

    def _on_exit(self, conv_id: str, proc: ChatProcess, expected: bool, stderr_tail: str) -> None:
        live = self._live.get(conv_id)
        if live is None or live.proc is not proc:
            return  # replaced by an edit or already stopped
        was_busy = live.state in BUSY
        conv = self._index.get(conv_id)
        agent = conv.agent if conv is not None else "claude"
        expected = expected or (agent == "grok" and live.result_seen)  # grok exits by itself once its turn is done
        live.proc = None
        self._clear_approvals(conv_id, live)
        live.partial = ""
        live.pending_cut = None  # an edit whose process died unwritten is lost: the next resume sees the old tip
        self._set_state(conv_id, "dormant")
        if not expected:
            text = f"{_process_name(agent)} 进程意外退出" + ("，本轮中断" if was_busy else "")
            self._notice(conv_id, f"{text}：{stderr_tail}" if stderr_tail else text)

    # ---- stream messages

    def _on_message(self, conv_id: str, proc: ChatProcess, msg: dict) -> None:
        """One line of a Claude Code or grok process's stream-json."""
        live = self._live.get(conv_id)
        if live is None or live.proc is not proc:
            return  # a detached process still draining its output
        kind = msg.get("type")
        if kind == "stream_event":
            self._on_stream_event(conv_id, live, msg.get("event") or {})
        elif kind in ("user", "assistant"):
            self._on_chat_message(conv_id, live, msg)
        elif kind == "system":
            self._on_system(conv_id, live, msg)
        elif kind == "rate_limit_event":
            self._on_rate_limit(conv_id, msg.get("rate_limit_info") or {})
        elif kind == "result":
            self._on_result(conv_id, live, msg)
        elif kind == "control_request":
            self._on_control_request(conv_id, live, msg)
        elif kind == "control_cancel_request":  # claude withdrew a prompt, e.g. on interrupt
            self._drop_approval(conv_id, live, msg.get("request_id"))
        elif kind == "control_response":
            self._on_control_response(conv_id, live, msg.get("response") or {})

    def _on_stream_event(self, conv_id: str, live: Live, event: dict) -> None:
        self._mark_busy(conv_id)
        self._set_retry(conv_id, None)  # the model is answering
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            live.partial += delta["text"]
            self._broadcast(conv_id, "delta", {"text": delta["text"]})

    def _on_chat_message(self, conv_id: str, live: Live, msg: dict) -> None:
        if self._agent(conv_id) == "grok":
            msg = self._grok_log(conv_id).message(msg)  # grok keeps no transcript the app can read
        if msg["type"] == "assistant":
            self._mark_busy(conv_id)
            # A subagent's messages come through the stream too, tagged with the Agent call that spawned them;
            # their usage is the subagent's own context, not this conversation's.
            tokens = None if msg.get("parent_tool_use_id") else context_tokens(msg["message"])
            if tokens:
                live.context_tokens = tokens
                self._broadcast(conv_id, "context", self._context(conv_id))
        self._show(conv_id, live, msg)

    def _show(self, conv_id: str, live: Live, msg: dict) -> None:
        """A transcript message's items to the open pages; tool results patch the call they answer."""
        for item in message_items(msg):
            if item["kind"] == "tool_result":
                self._broadcast(conv_id, "tool_result", item)
                continue
            if item["kind"] == "text":
                live.partial = ""
            self._broadcast(conv_id, "item", item)
            if item["kind"] == "tool":
                self._on_tool(conv_id, item)

    def _on_system(self, conv_id: str, live: Live, msg: dict) -> None:
        if msg.get("subtype") == "status":
            if msg.get("status") == "compacting":
                self._set_state(conv_id, "compacting")
            elif live.state == "compacting":  # a stray status outside a turn must not wedge "running"
                self._set_state(conv_id, "running")
        elif msg.get("subtype") == "compact_boundary":
            live.context_tokens = None
            self._broadcast(conv_id, "item", compact_item(msg["uuid"], msg.get("compact_metadata") or {}))
            self._broadcast(conv_id, "context", self._context(conv_id))
        elif msg.get("subtype") == "api_retry":
            # Nothing else shows while claude waits on the API, for minutes when it stalls.
            self._set_retry(conv_id, retry_text(msg))

    def _on_result(self, conv_id: str, live: Live, msg: dict) -> None:
        conv = self._index.get(conv_id)
        if conv.agent == "grok":
            live.result_seen = True  # even an error result: grok's exit after it is the normal end of the turn
            failure = _grok_failure(msg)
            if failure is not None:
                self._notice(conv_id, failure)
            cost = msg.get("total_cost_usd")
            changes = ({"cost_usd": (conv.cost_usd or 0.0) + cost}
                       if isinstance(cost, (int, float)) and not isinstance(cost, bool) else {})
        else:
            changes = self._claude_window(conv, msg)
        conv = self._index.update(conv_id, updated_at=self._wall(), **changes)
        live.partial = ""
        live.last_used = self._now()
        self._clear_approvals(conv_id, live)  # an interrupted turn whose cancel never arrived
        # A discover turn ends when grok exits (`_on_exit`): until then it still writes its session, so a
        # second resume, an eviction or a reap must not start.
        if conv.agent != "grok":
            self._set_state(conv_id, "idle")
        self._broadcast(conv_id, "context", self._context(conv_id))
        if "cost_usd" in changes:
            self._broadcast(conv_id, "conv", self._summary(conv))
        if live.title_from is not None:
            self._spawn_task(self._request_title(conv_id, live))

    @staticmethod
    def _claude_window(conv: Conversation, msg: dict) -> dict:
        # The result may list helper models too; the window that matters is the conversation's own model's.
        usage = msg.get("modelUsage") or {}
        own = [u for name, u in usage.items() if name.startswith(conv.model)]
        window = (own[0] if own else next(iter(usage.values()), {})).get("contextWindow")
        return {"context_window": window} if window else {}

    async def _request_title(self, conv_id: str, live: Live) -> None:
        text, live.title_from = live.title_from, None
        if live.proc is None:
            return
        live.title_request = f"title-{uuid.uuid4().hex[:8]}"  # set before writing: the answer may be quick
        try:
            await live.proc.generate_title(live.title_request, text)
        except OSError as e:  # the process died: the first-message title stays
            log.info("chat %s: title request not sent: %s", conv_id, e)

    def _on_control_response(self, conv_id: str, live: Live, response: dict) -> None:
        if live.title_request is None or response.get("request_id") != live.title_request:
            return  # an interrupt ack, or a title the user's rename superseded
        live.title_request = None
        title = (response.get("response") or {}).get("title")
        if isinstance(title, str) and title.strip():  # null when generation failed: keep the old title
            conv = self._index.update(conv_id, title=make_title(title))
            self._broadcast(conv_id, "conv", self._summary(conv))

    def _on_control_request(self, conv_id: str, live: Live, msg: dict) -> None:
        request, request_id = msg.get("request") or {}, msg.get("request_id")
        if request.get("subtype") != "can_use_tool" or not isinstance(request_id, str):
            return
        # Every prompt gets an answer, even a malformed one: claude waits on it forever otherwise.
        approval = self._skill_approval(conv_id, request_id, request)
        if approval is None:  # outside the whitelist and not a skill edit: refused as before the prompt tool
            # Claude Code's own reason names the rule hit (e.g. `cd … && git`), which the agent can adapt to.
            reason = request.get("decision_reason")
            message = (f"{AUTO_DENY_MESSAGE} Claude Code's reason: {reason}"
                       if isinstance(reason, str) and reason.strip() else AUTO_DENY_MESSAGE)
            self._spawn_task(self._respond(conv_id, live.proc, request_id, {"behavior": "deny", "message": message}))
            return
        live.approvals[request_id] = approval
        self._broadcast(conv_id, "approval", approval.public())

    def _skill_approval(self, conv_id: str, request_id: str, request: dict) -> Approval | None:
        """The phone card for a skill edit (`request` in Claude's can_use_tool shape); None for anything else."""
        root = self._launch.repo_root
        try:
            path = approvable_path(str(request.get("tool_name", "")), request.get("input") or {}, root)
            return new_approval(request_id, request, path, root) if path is not None else None
        except Exception:
            log.exception("chat %s: permission request %s not understood", conv_id, request_id)
            return None

    async def _respond(self, conv_id: str, proc: ChatProcess, request_id: str, response: dict) -> None:
        try:
            await proc.respond(request_id, response)
        except OSError as e:  # the process died: its exit already told the page
            log.info("chat %s: permission answer not sent: %s", conv_id, e)

    def _drop_approval(self, conv_id: str, live: Live, request_id: object) -> None:
        if live.approvals.pop(request_id, None) is not None:
            self._broadcast(conv_id, "approval_done", {"request_id": request_id})

    def _clear_approvals(self, conv_id: str, live: Live) -> None:
        for request_id in list(live.approvals):
            self._drop_approval(conv_id, live, request_id)

    def _on_rate_limit(self, conv_id: str, info: dict) -> None:
        windows = info.get("unifiedWindows") or {}
        self.usage = {"five_hour": (windows.get("five_hour") or {}).get("utilization"),
                      "seven_day": (windows.get("seven_day") or {}).get("utilization"),
                      "status": info.get("status"), "resets_at": info.get("resetsAt")}
        for cid in self._live:
            if self._agent(cid) == "claude":  # Claude's subscription windows say nothing about the other agents
                self._broadcast(cid, "usage", self.usage)
        if info.get("isUsingOverage"):
            self._notice(conv_id, "检测到超额计费（usage credits），已停止本轮")
            self._spawn_task(self.interrupt(conv_id))
        elif info.get("status") == "rejected":
            reset = info.get("resetsAt")
            when = datetime.fromtimestamp(reset).strftime("%m-%d %H:%M") if reset else "稍后"
            self._notice(conv_id, f"订阅额度已满，{when} 重置")

    def _on_tool(self, conv_id: str, item: dict) -> None:
        """A research conversation joins the ticker whose framework it writes first."""
        conv = self._index.get(conv_id)
        if conv is None or conv.kind != "research" or conv.ticker is not None or item["name"] not in ("Write", "Edit"):
            return
        ticker = framework_ticker(str(item["input"].get("file_path", "")), self._launch.repo_root)
        if ticker is not None:
            conv = self._index.update(conv_id, ticker=ticker)
            self._broadcast(conv_id, "conv", self._summary(conv))

    # ---- helpers

    def _mark_busy(self, conv_id: str) -> None:
        # A message queued while the previous turn ended starts a turn nobody announced.
        if self._live[conv_id].state == "idle":
            self._set_state(conv_id, "running")

    def _set_state(self, conv_id: str, state: str) -> None:
        live = self._live[conv_id]
        if state not in BUSY:
            self._set_retry(conv_id, None)  # the turn is over, however it ended
        if live.state != state:
            live.state = state
            self._broadcast(conv_id, "state", {"state": state})

    def _set_retry(self, conv_id: str, text: str | None) -> None:
        live = self._live[conv_id]
        if live.retry != text:
            live.retry = text
            self._broadcast(conv_id, "retry", {"text": text})

    def _notice(self, conv_id: str, text: str) -> None:
        self._broadcast(conv_id, "item", notice_item(text))

    def _broadcast(self, conv_id: str, event: str, data: dict) -> None:
        live = self._live.get(conv_id)
        for q in list(live.subscribers if live else ()):
            q.put_nowait((event, data))

    def _spawn_task(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("chat background task failed", exc_info=task.exception())

    def _context(self, conv_id: str) -> dict:
        conv = self._index.get(conv_id)
        return {"tokens": self._live[conv_id].context_tokens, "window": conv.context_window if conv else None}

    def _summary(self, conv: Conversation) -> dict:
        live = self._live.get(conv.id)
        # Grok's label is its model id, which the page already shows (grok's catalog is a file read per row).
        labels = dict(CHAT_MODELS if conv.agent == "claude" else OPENCODE_MODELS if conv.agent == "opencode" else ())
        return {**asdict(conv), "state": live.state if live else "dormant", "agent_label": AGENT_LABELS[conv.agent],
                "model_label": labels.get(conv.model, conv.model)}

    def _agent(self, conv_id: str) -> str | None:
        conv = self._index.get(conv_id)
        return conv.agent if conv is not None else None

    def _require_grok(self) -> GrokConfig:
        if self._grok is None:
            raise Invalid("Grok 未配置")
        return self._grok

    def _require_opencode(self) -> OpencodeConfig:
        if self._opencode is None:
            raise Invalid("opencode 未配置")
        return self._opencode

    def _grok_log(self, conv_id: str) -> AppLog:
        if conv_id not in self._grok_logs:
            self._grok_logs[conv_id] = AppLog(self._transcript(conv_id))
        return self._grok_logs[conv_id]

    def _broadcast_entry(self, conv_id: str, entry: dict) -> None:
        for item in message_items(entry):
            self._broadcast(conv_id, "item", item)

    def _transcript(self, conv_id: str) -> Path:
        agent = self._agent(conv_id)
        if agent == "grok":
            return self._require_grok().logs / f"{conv_id}.jsonl"
        if agent == "opencode":
            return self._require_opencode().logs / f"{conv_id}.jsonl"
        return self._projects / f"{conv_id}.jsonl"

    def _visible(self, conv: Conversation) -> bool:
        return not conv.archived and (conv.id in self._live or self._transcript(conv.id).exists())

    def _check_not_archived(self, conv_id: str, live: Live) -> None:
        """Under the chat's lock: an archive that ran while we waited for it has dropped `live`."""
        conv = self._index.get(conv_id)
        if self._live.get(conv_id) is not live or conv is None or conv.archived:
            raise NotFound(conv_id)

    def _conv(self, conv_id: str) -> Conversation:
        conv = self._index.get(conv_id)
        if conv is None or not self._visible(conv):
            raise NotFound(conv_id)
        return conv
