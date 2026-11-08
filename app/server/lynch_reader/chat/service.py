"""Chats: one Claude Code or opencode process per active conversation, or one grok process per discover turn; their state, and fan-out to the app's event streams.

Everything here runs on the server's event loop; nothing blocks it except small file reads.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from lynch_reader.chat import transcript
from lynch_reader.chat.approvals import (AUTO_DENY_MESSAGE, USER_DENY_MESSAGE, Approval, approvable_path,
                                         new_approval)
from lynch_reader.chat.candidates import CandidateStore
from lynch_reader.chat.codex import DEFAULT_CODEX_EFFORT, DEFAULT_CODEX_MODEL, CodexConfig, CodexModel, codex_env
from lynch_reader.chat.codex_link import CodexLink, CodexSetupError, discover_models
from lynch_reader.chat.events import LinkEvents
from lynch_reader.chat.watch import WatchStore
from lynch_reader.chat.config import (CHAT_EFFORTS, CHAT_MODELS, DEFAULT_EFFORT, DEFAULT_MODEL, IDLE_TIMEOUT_S,
                                      MAX_PROCESSES, LaunchConfig, launch_argv, launch_env)
from lynch_reader.chat.grok import (DEFAULT_GROK_MODEL, DEFAULT_RESEARCH_EFFORT, DEFAULT_RESEARCH_MODEL, DEFAULT_WINDOW,
                                    MAX_TURNS, GrokConfig, GrokSetupError, discover_prompt, grok_argv, grok_env,
                                    grok_models, new_session, prepare_workdir, research_env, write_research_home)
from lynch_reader.chat.grok_link import GrokLink
from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.index import ChatIndex, Conversation, make_title
from lynch_reader.chat.items import (compact_item, context_tokens, framework_ticker, message_items, notice_item,
                                     retry_text)
from lynch_reader.chat.kol import (DEFAULT_LOOKBACK_DAYS, LOOKBACK_DAYS, KolSources, KolUnavailable, kol_brief,
                                   kol_sources)
from lynch_reader.chat.opencode import (DEFAULT_OPENCODE_MODEL, OPENCODE_EFFORT, OPENCODE_MODELS, OpencodeConfig,
                                        opencode_env, write_rules)
from lynch_reader.chat.opencode_link import OpencodeLink, OpencodeSetupError
from lynch_reader.chat.opencode_http import OpencodeHttpError
from lynch_reader.chat.process import ChatProcess
from lynch_reader.chat.queue import MessageQueue
from lynch_reader.chat.skills import FinanceSkills, FinanceSkillsError

log = logging.getLogger(__name__)

BUSY = ("running", "compacting")
COMPACT_COMMAND = "/compact"
KINDS = ("research", "ticker", "discover")
AGENT_LABELS = {"claude": "Claude Code", "opencode": "opencode", "grok": "Grok", "codex": "Codex"}
CODEX_MODELS_TTL_S = 300.0
DEFAULT_AGENT = "codex"


def _process_name(agent: str) -> str:
    """How notices about a chat's process name it; Claude's predate the agent labels."""
    return "Claude" if agent == "claude" else AGENT_LABELS[agent]


def _result_failure(result: dict, agent: str) -> str | None:
    """A stream-json result can be the only place the agent reports a failure."""
    subtype = str(result.get("subtype") or "")
    failed = (result.get("is_error") in (True, "True") or subtype.startswith("error")
              or (agent == "grok" and subtype != "success"))
    if not failed:
        return None
    if agent == "grok" and subtype == "error_max_turns":
        return (f"Grok 达到单轮步数上限（{MAX_TURNS} 步），本轮提前结束。"
                "可以缩小搜索范围再试：少看几位 KOL，或缩短回看时长。")
    errors = result.get("errors")
    detail = "；".join(map(str, errors)) if isinstance(errors, list) else str(result.get("result") or "")
    detail = detail.strip()[:300]
    label = _process_name(agent)
    return f"{label} 本轮出错结束：{detail}" if detail else f"{label} 本轮出错结束"


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Invalid(Exception):
    pass


@dataclass
class Live:
    """Runtime state of one conversation. No entry means dormant with nobody watching."""
    generation: int = 0
    archiving: bool = False
    operations: set[asyncio.Task] = field(default_factory=set)
    starting: list = field(default_factory=list)
    state: str = "dormant"  # running | compacting | idle | dormant
    proc: ChatProcess | OpencodeLink | GrokLink | CodexLink | None = None
    last_used: float = 0.0
    partial: str = ""  # streamed text not yet closed by its full message
    retry: str | None = None  # the API request being retried, until the model answers or the turn ends
    context_tokens: int | None = None
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    # A new conversation's first message, until its first reply ends and claude is asked for a title;
    # then the id of that request, whose answer replaces the title unless the user renamed meanwhile.
    title_from: str | None = None
    title_request: str | None = None
    native_title: str | None = None
    first_completed: bool = False
    limits: dict | None = None
    # (resume uuid, transcript entry count) of an edit whose new process has not written yet;
    # until it does, the file's newest message is still the edited-away branch.
    pending_cut: tuple[str, int] | None = None
    # Discover only: this turn's grok printed its `result`, so its exit is the normal end of the turn.
    result_seen: bool = False
    # Skill edits the agent is waiting on the phone to allow or deny, by request id, in arrival order.
    # They belong to `proc`: whatever ends it (exit, stop, edit, archive) drops them.
    approvals: dict[str, Approval] = field(default_factory=dict)
    framework_writes: dict[str, str] = field(default_factory=dict)  # tool id → ticker, awaiting a successful result
    interrupted: bool = False  # Claude already displayed its native stop marker for this turn
    # Held by public commands that spawn or stop this chat's own process (send, edit, archive),
    # so two of them never race on `proc`. `_spawn` / `_stop` / `_make_room` never take it:
    # `_make_room` stops other chats' idle processes without their lock, so no lock-order deadlock.
    # Eviction (`_make_room`, `reap_idle`) skips a chat whose lock is held: a command is writing to it.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ChatService:
    def __init__(self, index: ChatIndex, launch: LaunchConfig, projects_dir: Path, base_env: Mapping[str, str], *,
                 grok: GrokConfig | None = None, candidates: CandidateStore | None = None,
                 watch: WatchStore | None = None, opencode: OpencodeConfig | None = None,
                 codex: CodexConfig | None = None,
                 skills: FinanceSkills | None = None,
                 monotonic: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 idle_timeout_s: float = IDLE_TIMEOUT_S, max_processes: int = MAX_PROCESSES) -> None:
        self._index = index
        self._queue = MessageQueue(index.path.with_name("queue.json"))
        self._closing = False
        self._launch = launch
        self._projects = projects_dir
        self._env = launch_env(base_env)
        self._grok = grok
        self._grok_env = grok_env(base_env)
        self._app_logs: dict[str, AppLog] = {}
        self._opencode = opencode
        self._opencode_env = dict(base_env)  # opencode bills its own providers; the Claude billing guard is moot
        self._codex = codex
        self._skills = skills
        self._codex_env = codex_env(base_env)
        self._codex_models: list[CodexModel] = []
        self._codex_hint: str | None = "Codex 模型清单尚未读取" if codex else "Codex 未配置"
        self._codex_models_at: float | None = None
        self._codex_models_lock = asyncio.Lock()
        self.candidates = candidates
        self.watch = watch
        self._now, self._wall = monotonic, wall
        self._idle_timeout_s, self._max_processes = idle_timeout_s, max_processes
        self._live: dict[str, Live] = {}
        self._tasks: set[asyncio.Task] = set()
        self.usage: dict | None = None

    # ---- queries

    def options(self) -> dict:
        discover_models = grok_models(self._grok.home) if self._grok else []
        research_models = grok_models(self._grok.home, search_only=False) if self._grok else []
        discover = self._grok_block(discover_models, DEFAULT_GROK_MODEL, lookback=True)
        grok = self._grok_block(research_models, DEFAULT_RESEARCH_MODEL, lookback=False)
        return {"models": [{"id": m, "label": label} for m, label in CHAT_MODELS], "efforts": list(CHAT_EFFORTS),
                "default_model": DEFAULT_MODEL, "default_effort": DEFAULT_EFFORT, "discover": discover,
                "agents": [{"id": a, "label": AGENT_LABELS[a]} for a in ("grok", "claude", "opencode", "codex")],
                "default_agent": DEFAULT_AGENT, "grok": grok,
                "opencode": {"models": [{"id": m, "label": label} for m, label in OPENCODE_MODELS],
                             "default_model": DEFAULT_OPENCODE_MODEL},
                "codex": {"models": [m.public() for m in self._codex_models],
                          "default_model": self._codex_default().id if self._codex_models else None,
                          "hint": self._codex_hint}}

    async def refresh_codex_models(self) -> None:
        """Probe the local account/catalog at most once per five minutes, including failed probes."""
        if self._codex is None:
            return
        async with self._codex_models_lock:
            if self._codex_models_at is not None and self._now() - self._codex_models_at < CODEX_MODELS_TTL_S:
                return
            try:
                self._codex_models = await discover_models(self._codex, self._codex_env)
                self._codex_models = [
                    replace(m, default_effort=DEFAULT_CODEX_EFFORT)
                    if m.id == DEFAULT_CODEX_MODEL and DEFAULT_CODEX_EFFORT in m.efforts else m
                    for m in self._codex_models]
                self._codex_hint = None if self._codex_models else "Codex 没有可用模型，请在终端检查 codex 登录与模型配置"
            except (CodexSetupError, OSError) as e:
                self._codex_models = []
                self._codex_hint = f"无法读取 Codex 模型：{e}"
            self._codex_models_at = self._now()

    def _codex_default(self) -> CodexModel:
        return next((m for m in self._codex_models if m.id == DEFAULT_CODEX_MODEL),
                    next((m for m in self._codex_models if m.is_default), self._codex_models[0]))

    def _grok_block(self, models: list, preferred: str, *, lookback: bool) -> dict:
        default = next((m for m in models if m.id == preferred), models[0] if models else None)
        window = (DEFAULT_WINDOW if default is None or DEFAULT_WINDOW in default.windows
                  else default.windows[0])
        effort = (DEFAULT_RESEARCH_EFFORT if default and DEFAULT_RESEARCH_EFFORT in default.efforts
                  else (default.default_effort if default else "high"))
        out = {"models": [m.public() for m in models], "default_model": default.id if default else None,
               "default_window": window, "default_effort": effort,
               "hint": None if models else "在终端运行 grok models 刷新 Grok 模型清单"}
        if lookback:
            out["lookback_days"] = list(LOOKBACK_DAYS)
            out["default_lookback_days"] = DEFAULT_LOOKBACK_DAYS
            out.pop("default_effort", None)  # discover picker has its own effort on each model
        return out

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
                "approvals": [a.public() for a in live.approvals.values()],
                "queued": self._queue.pending(conv_id) if conv.agent != "claude" else None,
                "limits": live.limits}

    # ---- commands

    async def create(self, kind: str, ticker: str | None, text: str, model: str | None, effort: str | None,
                     context_window: object = None, lookback_days: object = None,
                     agent: str | None = None, client_id: str | None = None) -> Conversation:
        self._check_open()
        self._validate_client_id(client_id)
        # The values come from a JSON body; a list or dict would break the membership tests below.
        if (not all(isinstance(v, str) for v in (kind, text))
                or not all(isinstance(v, str | None) for v in (model, effort))
                or not all(isinstance(v, str | None) for v in (ticker, agent))):
            raise Invalid("fields must be strings")
        if kind not in KINDS:
            raise Invalid(f"unknown kind {kind!r}")
        if not text.strip():
            raise Invalid("empty message")
        # 发现新票 runs only on Grok; 研究 / 讨论 use the app's default agent.
        agent = agent or ("grok" if kind == "discover" else DEFAULT_AGENT)
        allowed = ("grok",) if kind == "discover" else ("grok", "claude", "opencode", "codex")
        if agent not in allowed:
            raise Invalid(f"agent {agent!r} cannot run a {kind} chat")
        if agent == "codex":
            conv = await self._new_codex(kind, ticker, text, model, effort)
        elif not all(isinstance(v, str) for v in (model, effort)):
            raise Invalid("fields must be strings")
        elif kind == "discover":
            conv = await self._new_discover(text, model, effort, context_window, lookback_days)
        elif agent == "grok":
            conv = self._new_research_grok(kind, ticker, text, model, effort, context_window)
        elif agent == "opencode":
            conv = self._new_opencode(kind, ticker, text, model, effort)
        else:
            conv = self._new_claude(kind, ticker, text, model, effort)
        self._check_open()
        self._check_open()
        self._index.put(conv)
        # Visible in the list before its transcript exists. Claude titles a chat after its first reply; the others
        # keep the first message.
        self._live[conv.id] = Live(title_from=text)
        await self.send(conv.id, text, client_id)
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

    def _new_research_grok(self, kind: str, ticker: str | None, text: str, model: str, effort: str,
                           window: object) -> Conversation:
        grok = self._require_grok()
        chosen = next((m for m in grok_models(grok.home, search_only=False) if m.id == model), None)
        if chosen is None:
            raise Invalid(f"unknown Grok model {model!r}")
        if effort not in chosen.efforts:
            raise Invalid(f"unknown effort {effort!r}")
        if window is None:
            window = DEFAULT_WINDOW if DEFAULT_WINDOW in chosen.windows else chosen.windows[0]
        if type(window) is not int or window not in chosen.windows:
            raise Invalid(f"unknown context window {window!r}")
        if kind == "ticker" and not ticker:
            raise Invalid("a ticker conversation needs a ticker")
        now = self._wall()
        return Conversation(id=str(uuid.uuid4()), kind=kind, ticker=ticker if kind == "ticker" else None,
                            title=make_title(text), model=model, effort=effort, created_at=now, updated_at=now,
                            context_window=window, agent="grok")

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

    async def _new_codex(self, kind: str, ticker: str | None, text: str, model: str | None,
                         effort: str | None) -> Conversation:
        self._require_codex()
        if kind == "ticker" and not ticker:
            raise Invalid("a ticker conversation needs a ticker")
        await self.refresh_codex_models()
        if not self._codex_models:
            raise Conflict(self._codex_hint or "Codex 没有可用模型")
        model = model or self._codex_default().id
        chosen = next((m for m in self._codex_models if m.id == model), None)
        if chosen is None:
            raise Invalid(f"unknown Codex model {model!r}")
        effort = effort or chosen.default_effort
        if effort not in chosen.efforts:
            raise Invalid(f"unknown Codex effort {effort!r}")
        now = self._wall()
        return Conversation(id=str(uuid.uuid4()), kind=kind, ticker=ticker if kind == "ticker" else None,
                            title=make_title(text), model=model, effort=effort, created_at=now, updated_at=now,
                            agent="codex")

    @staticmethod
    def _validate_client_id(client_id: str | None) -> None:
        if client_id is not None and (not isinstance(client_id, str) or not client_id.strip()):
            raise Invalid("invalid message id")

    def _checkpoint(self, conv_id: str, live: Live, generation: int) -> None:
        conv = self._index.get(conv_id)
        if live.archiving or self._live.get(conv_id) is not live or conv is None or conv.archived:
            raise NotFound(conv_id)
        if self._closing or live.generation != generation:
            raise Conflict("本次操作已停止，输入已保留")

    def _locked_conv(self, conv_id: str, live: Live, generation: int) -> Conversation:
        """Resolve native context only after the command owns the chat lock."""
        self._checkpoint(conv_id, live, generation)
        self._check_not_archived(conv_id, live)
        return self._conv(conv_id)

    async def _start_owned(self, conv_id: str, live: Live, generation: int, resource, start):
        self._checkpoint(conv_id, live, generation)
        live.starting.append(resource)
        try:
            result = await start()
            self._checkpoint(conv_id, live, generation)
            return result
        except BaseException:
            await resource.close()
            raise
        finally:
            live.starting.remove(resource)

    async def _check_send_current(self, conv_id: str, live: Live, generation: int) -> None:
        try:
            self._checkpoint(conv_id, live, generation)
        except (Conflict, NotFound):
            if self._live.get(conv_id) is live:
                await self._stop(conv_id)
            raise

    async def _cancel_operations(self, live: Live) -> None:
        live.generation += 1
        current = asyncio.current_task()
        tasks = [task for task in live.operations if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(resource.close() for resource in list(live.starting)), return_exceptions=False)

    async def send(self, conv_id: str, text: str, client_id: str | None = None) -> None:
        self._validate_client_id(client_id)
        self._conv(conv_id)
        live = self._live.setdefault(conv_id, Live())
        generation = live.generation
        task = asyncio.create_task(self._send(conv_id, text, client_id))
        live.operations.add(task)
        try:
            await task
            self._checkpoint(conv_id, live, generation)
        except asyncio.CancelledError:
            conv = self._index.get(conv_id)
            if live.archiving or self._live.get(conv_id) is not live or conv is None or conv.archived:
                raise NotFound(conv_id) from None
            if self._closing:
                raise Conflict("对话服务已关闭，本次输入已保留") from None
            if live.generation != generation:
                raise Conflict("本次发送已停止，输入已保留") from None
            raise
        finally:
            live.operations.discard(task)

    async def _send(self, conv_id: str, text: str, client_id: str | None = None) -> None:
        self._check_open()
        self._conv(conv_id)
        if not text.strip():
            raise Invalid("empty message")
        self._validate_client_id(client_id)
        live = self._live.setdefault(conv_id, Live())
        generation = live.generation
        async with live.lock:
            conv = self._locked_conv(conv_id, live, generation)
            if conv.agent == "claude":
                # Claude already owns its native interjection queue; preserve that behavior.
                if text.lstrip().startswith(COMPACT_COMMAND) and live.state in BUSY:
                    raise Conflict("等这一轮结束再压缩")
                generation = live.generation
                if live.proc is None or not live.proc.alive:
                    await self._spawn(conv)
                await self._check_send_current(conv_id, live, generation)
                await self._write_user(conv_id, text)
                return
            if text.lstrip().split(maxsplit=1)[0] == COMPACT_COMMAND:
                if live.state in BUSY or self._queue.pending(conv_id):
                    raise Conflict("先结束当前回答并处理待发送消息，再压缩")
                await self._send_now(conv, live, text)
                return
            try:
                row = self._queue.add(conv_id, text, client_id)
            except ValueError as error:
                raise Invalid(str(error)) from error
            self._queue_changed(conv_id)
            if row["state"] != "queued" or live.state in BUSY:
                return
            await self._send_pending(conv, live)

    async def _send_now(self, conv: Conversation, live: Live, text: str) -> None:
        if conv.agent == "grok" and conv.kind == "discover":
            await self._start_grok_turn(conv, live, text)
        else:
            await self._send_link(conv, live, text)

    async def _send_pending(self, conv: Conversation, live: Live) -> None:
        row = self._queue.next(conv.id)
        if row is None or live.state in BUSY or self._closing:
            return
        self._queue.begin(conv.id, row["id"])
        self._queue_changed(conv.id)
        log = self._app_log(conv.id)
        log.client_id = row["id"]
        try:
            if asyncio.current_task() in live.operations:
                await self._send_now(conv, live, row["text"])
            else:
                # Background dequeue has the same cancellation boundary as an HTTP send.
                operation = asyncio.create_task(self._send_now(conv, live, row["text"]))
                live.operations.add(operation)
                try:
                    await operation
                finally:
                    live.operations.discard(operation)
        except BaseException:
            self._pause_inputs(conv.id)
            raise
        finally:
            log.client_id = None

    async def _drain_queue(self, conv_id: str) -> None:
        if self._closing:
            return
        live = self._live.get(conv_id)
        if live is None:
            return
        async with live.lock:
            conv = self._index.get(conv_id)
            if conv is None or conv.archived or self._live.get(conv_id) is not live:
                return
            try:
                await self._send_pending(self._conv(conv_id), live)
            except (Conflict, Invalid, OSError) as error:
                self._notice(conv_id, f"待发送消息未能开始：{error}")

    def _queue_changed(self, conv_id: str) -> None:
        self._broadcast(conv_id, "queued", {"queued": self._queue.pending(conv_id)})

    def _confirm_input(self, conv_id: str) -> None:
        row = self._queue.active(conv_id)
        if row is not None:
            try:
                self._queue.complete(conv_id, row["id"])
            except OSError as error:
                self._pause_inputs(conv_id, error=error)
                return
            self._queue_changed(conv_id)

    def _reconcile_inputs(self, conv_id: str) -> None:
        try:
            self._reconcile_inputs_unchecked(conv_id)
        except OSError as error:
            self._pause_inputs(conv_id, error=error)

    def _reconcile_inputs_unchecked(self, conv_id: str) -> None:
        uncertain = {row["id"] for row in self._queue.pending(conv_id) if row["status"] == "uncertain"}
        if not uncertain:
            return
        log = self._app_log(conv_id)
        accepted = {entry.get("client_id") for entry in log.entries()
                    if entry.get("client_id") in uncertain and log.native_id(entry["uuid"]) is not None}
        for message_id in accepted:
            self._queue.complete(conv_id, message_id)
        if accepted:
            self._queue_changed(conv_id)

    def _pause_inputs(self, conv_id: str, *, error: OSError | None = None) -> None:
        try:
            self._queue.pause(conv_id)
            if error is None:
                self._reconcile_inputs_unchecked(conv_id)
        except OSError as failure:
            error = error or failure
        if error is not None:
            log.warning("chat %s: input bookkeeping could not be persisted: %s", conv_id, error)
            self._notice(conv_id, "消息暂停状态未能保存，排队内容已停止；请检查磁盘后再试")
        self._queue_changed(conv_id)

    def _next_input(self, conv_id: str) -> None:
        if not self._closing and self._queue.next(conv_id) is not None:
            self._spawn_task(self._drain_queue(conv_id))

    def dismiss_queued(self, conv_id: str, message_id: str) -> None:
        self._conv(conv_id)
        self._queue.dismiss(conv_id, message_id)
        self._queue_changed(conv_id)

    async def interrupt(self, conv_id: str) -> None:
        conv = self._conv(conv_id, repair=False)
        live = self._live.get(conv_id)
        if live is not None:
            await self._cancel_operations(live)
        self._pause_inputs(conv_id)
        if not (live and live.proc and live.proc.alive and live.state in BUSY):
            return
        if conv.agent == "grok" and conv.kind == "discover":
            await self._stop_grok_turn(conv_id, live)
            return
        try:
            await live.proc.interrupt()  # Claude's interrupt message, or opencode's session/cancel
        except (BrokenPipeError, ConnectionResetError) as e:  # it exited after the alive check
            raise Conflict(f"{_process_name(conv.agent)} 进程已退出，原因见对话里的提示") from e

    async def edit(self, conv_id: str, message_uuid: str, text: str, client_id: str | None = None) -> None:
        """Truncate the conversation before `message_uuid` and send `text` in its place. Files are not touched."""
        self._validate_client_id(client_id)
        self._conv(conv_id)
        live = self._live.setdefault(conv_id, Live())
        generation = live.generation
        async with live.lock:
            conv = self._locked_conv(conv_id, live, generation)
            if live.state in BUSY:
                raise Conflict("回答进行中，先停止再编辑")
            if not text.strip():
                raise Invalid("empty message")
            if self._queue.pending(conv_id):
                raise Conflict("先处理待发送消息，再编辑历史")
            path = self._transcript(conv_id)
            entries = transcript.read_entries(path) if path.exists() else []
            try:
                cut = transcript.edit_cut(transcript.current_branch(entries), message_uuid)
            except transcript.EditRejected as e:
                raise Invalid(str(e)) from e
            if conv.agent != "claude":
                # Persist the replacement before touching native history. A partial edit remains a draft,
                # and cannot be automatically sent into whichever branch survived a failed commit.
                self._queue.add(conv_id, text, client_id)
                self._queue_changed(conv_id)
                try:
                    if conv.kind == "discover":
                        await self._spawn_discover_control(conv, live)
                    elif live.proc is None or not live.proc.alive:
                        spawn = {"codex": self._spawn_codex, "grok": self._spawn_research_grok,
                                 "opencode": self._spawn_opencode}[conv.agent]
                        await spawn(conv)
                    self._checkpoint(conv_id, live, generation)
                    session = await live.proc.rewind(message_uuid)
                    self._checkpoint(conv_id, live, generation)
                    self._app_log(conv_id).branch_at(cut, agent=conv.agent, native_session=session,
                                                     native_ids=getattr(live.proc, "fork_native_ids", None))
                    field = {"codex": "codex_thread", "grok": "grok_session", "opencode": "opencode_session"}[conv.agent]
                    conv = self._index.update(conv_id, **{field: session})
                except asyncio.CancelledError:
                    self._pause_inputs(conv_id)
                    await self._stop(conv_id)
                    self._app_logs.pop(conv_id, None)
                    raise
                except Exception as error:
                    self._pause_inputs(conv_id)
                    await self._stop(conv_id)
                    self._app_logs.pop(conv_id, None)
                    if isinstance(error, (Conflict, CodexSetupError, GrokSetupError, OpencodeSetupError,
                                          OpencodeHttpError, OSError, ValueError)):
                        raise Conflict(f"无法编辑对话，替换文字已保留为未回答草稿：{error}") from error
                    raise
                live.context_tokens = None
                self._broadcast(conv_id, "reset", {})
                if conv.kind == "discover":
                    await self._stop(conv_id)
                await self._send_pending(conv, live)
                return
            await self._stop(conv_id)
            self._checkpoint(conv_id, live, generation)
            await self._spawn(conv, resume_at=cut)
            await self._check_send_current(conv_id, live, generation)
            live.pending_cut = (cut, len(entries))
            live.context_tokens = None  # the pre-edit count belongs to the dropped turns
            self._broadcast(conv_id, "reset", {})  # the branch changed: watchers refetch a snapshot
            await self._write_user(conv_id, text)

    async def archive(self, conv_id: str) -> None:
        """Hide the conversation; its process dies at once, whatever it was doing."""
        self._conv(conv_id, repair=False)
        live = self._live.setdefault(conv_id, Live())
        live.archiving = True
        await self._cancel_operations(live)
        async with live.lock:
            self._pause_inputs(conv_id)
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
            if conv.agent in ("opencode", "grok", "codex"):
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
        self._closing = True
        for live in list(self._live.values()):
            await self._cancel_operations(live)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._skills is not None:
            await self._skills.close()
        for conv_id in list(self._live):
            self._pause_inputs(conv_id)
            await self._stop(conv_id)

    # ---- process lifecycle

    async def _prepare_research_skills(self) -> None:
        if self._skills is not None:
            try:
                await self._skills.refresh()
            except FinanceSkillsError as error:
                raise Conflict(str(error)) from error

    async def _spawn(self, conv: Conversation, resume_at: str | None = None) -> None:
        live = self._live.setdefault(conv.id, Live())
        generation = live.generation
        self._checkpoint(conv.id, live, generation)
        await self._prepare_research_skills()
        self._checkpoint(conv.id, live, generation)
        await self._make_room()
        self._checkpoint(conv.id, live, generation)
        resume = self._transcript(conv.id).exists()
        argv = launch_argv(self._launch, conv, resume=resume, resume_at=resume_at)
        proc = ChatProcess(argv, self._env, self._launch.repo_root,
                           on_message=lambda msg: self._on_message(conv.id, proc, msg),
                           on_exit=lambda p, expected, err: self._on_exit(conv.id, p, expected, err))
        try:
            await self._start_owned(conv.id, live, generation, proc, proc.start)
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
        generation = live.generation
        self._checkpoint(conv.id, live, generation)
        if live.state in BUSY:
            raise Conflict("回答进行中，等这一轮结束或先停止")
        if text.lstrip().split(maxsplit=1)[0] == COMPACT_COMMAND:
            await self._spawn_discover_control(conv, live)
            await self._check_send_current(conv.id, live, generation)
            self._set_state(conv.id, "compacting")
            try:
                await live.proc.compact(text.strip()[len(COMPACT_COMMAND):].strip())
            except asyncio.CancelledError:
                await self._stop(conv.id)
                raise
            except (GrokSetupError, OSError) as error:
                await self._stop(conv.id)
                raise Conflict(f"Grok 无法压缩对话：{error}") from error
            return
        if live.proc is not None:
            await self._stop(conv.id)
        grok = self._require_grok()
        skill = self._discover_skill()  # both read every turn: an edit applies at once
        kol = await self._kol_sources()
        self._checkpoint(conv.id, live, generation)
        days = conv.lookback_days or DEFAULT_LOOKBACK_DAYS  # chats made before the choice existed
        prompt = discover_prompt(skill, kol_brief(kol, days, datetime.fromtimestamp(self._wall(), UTC)), text)
        await self._make_room()
        self._checkpoint(conv.id, live, generation)
        try:
            prepare_workdir(grok.workdir)
        except GrokSetupError as e:
            raise Conflict(f"无法启动 Grok：{e}") from e
        # grok never echoes the prompt: the app records and shows it itself.
        self._broadcast_entry(conv.id, self._app_log(conv.id).user(text))
        proc = ChatProcess(grok_argv(grok, conv, prompt), self._grok_env, grok.workdir,
                           on_message=lambda msg: self._on_message(conv.id, proc, msg),
                           on_exit=lambda p, expected, err: self._on_exit(conv.id, p, expected, err))
        try:
            await self._start_owned(conv.id, live, generation, proc, proc.start)
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
                self._broadcast_entry(conv_id, self._app_log(conv_id).interrupted())

    async def _send_link(self, conv: Conversation, live: Live, text: str) -> None:
        """Start one turn under the chat lock; completion callbacks own its ending state."""
        label = AGENT_LABELS[conv.agent]
        generation = live.generation
        self._checkpoint(conv.id, live, generation)
        if live.state in BUSY:
            raise Conflict("回答进行中，等这一轮结束或先停止")
        compact = text.lstrip().split(maxsplit=1)[0] == COMPACT_COMMAND
        if live.proc is None or not live.proc.alive:
            spawn = {"opencode": self._spawn_opencode, "grok": self._spawn_research_grok,
                     "codex": self._spawn_codex}[conv.agent]
            await spawn(conv)
        await self._check_send_current(conv.id, live, generation)
        link = live.proc
        self._index.update(conv.id, updated_at=self._wall())
        live.last_used = self._now()
        self._set_state(conv.id, "compacting" if compact else "running")
        try:
            if compact:
                await link.compact(text.strip()[len(COMPACT_COMMAND):].strip())
            else:
                await link.prompt(text)
        except BaseException as e:
            # A failed/cancelled write may already have delivered the prompt. Close that uncertain turn
            # before another send, and never overwrite a completion callback after a successful write.
            if live.proc is link:
                await self._stop(conv.id)
            if isinstance(e, (CodexSetupError, GrokSetupError, OpencodeSetupError, OpencodeHttpError, OSError)):
                raise Conflict(f"{label} 无法发送消息：{e}") from e
            raise

    async def _spawn_opencode(self, conv: Conversation) -> None:
        live = self._live.setdefault(conv.id, Live())
        generation = live.generation
        self._checkpoint(conv.id, live, generation)
        cfg = self._require_opencode()
        await self._prepare_research_skills()
        self._checkpoint(conv.id, live, generation)
        await self._make_room()
        self._checkpoint(conv.id, live, generation)
        env = opencode_env(self._opencode_env, write_rules(cfg, conv))
        link = OpencodeLink(cfg, conv, env, self._app_log(conv.id), self._link_events(conv.id, lambda: link))
        try:
            session = await self._start_owned(conv.id, live, generation, link, link.start)
        except OpencodeSetupError as e:
            raise Conflict(f"无法启动 opencode：{e}") from e
        if conv.opencode_session != session:
            self._index.update(conv.id, opencode_session=session)
        self._clear_approvals(conv.id, live)  # as in _spawn: a dead predecessor's cards go now
        live.proc = link
        live.partial = ""
        live.last_used = self._now()
        self._set_state(conv.id, "idle")

    async def _spawn_research_grok(self, conv: Conversation) -> None:
        live = self._live.setdefault(conv.id, Live())
        generation = live.generation
        self._checkpoint(conv.id, live, generation)
        cfg = self._require_grok()
        await self._prepare_research_skills()
        self._checkpoint(conv.id, live, generation)
        await self._make_room()
        self._checkpoint(conv.id, live, generation)
        home = write_research_home(cfg, conv, cfg.home / ".grok")
        env = research_env(self._env, home)
        link = GrokLink(cfg, conv, env, self._app_log(conv.id),
                        self._link_events(conv.id, lambda: link), self._launch.repo_root)
        try:
            session = await self._start_owned(conv.id, live, generation, link, link.start)
        except GrokSetupError as e:
            raise Conflict(f"无法启动 Grok：{e}") from e
        if conv.grok_session != session:
            conv = self._index.update(conv.id, grok_session=session)
        self._clear_approvals(conv.id, live)
        live.proc = link
        live.partial = ""
        live.last_used = self._now()
        self._set_state(conv.id, "idle")

    async def _spawn_codex(self, conv: Conversation) -> None:
        live = self._live.setdefault(conv.id, Live())
        generation = live.generation
        self._checkpoint(conv.id, live, generation)
        cfg = self._require_codex()
        await self._prepare_research_skills()
        self._checkpoint(conv.id, live, generation)
        await self._make_room()
        self._checkpoint(conv.id, live, generation)
        link = CodexLink(cfg, conv, self._codex_env, self._app_log(conv.id),
                         self._link_events(conv.id, lambda: link))
        try:
            thread = await self._start_owned(conv.id, live, generation, link, link.start)
        except (CodexSetupError, OSError) as e:
            await link.close()
            raise Conflict(f"无法启动 Codex：{e}") from e
        if conv.codex_thread != thread:
            self._index.update(conv.id, codex_thread=thread)
        self._clear_approvals(conv.id, live)
        live.proc = link
        live.partial = ""
        live.last_used = self._now()
        self._set_state(conv.id, "idle")

    def _link_events(self, conv_id: str, link: Callable[[], OpencodeLink | GrokLink | CodexLink]) -> LinkEvents:
        """The link's callbacks, each ignored once the link is no longer the chat's (stopped or replaced)."""
        def current(handler: Callable[..., None]) -> Callable[..., None]:
            def run(*args) -> None:
                live = self._live.get(conv_id)
                if live is not None and live.proc is link():
                    handler(live, *args)
            return run

        def on_delta(live: Live, text: str) -> None:
            self._confirm_input(conv_id)
            self._set_retry(conv_id, None)
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
            self._set_retry(conv_id, None)
            failed = error is not None or stop in ("failed", "cancelled", "interrupted")
            if failed:
                self._pause_inputs(conv_id)
            else:
                self._confirm_input(conv_id)
                live.first_completed = True
            # An error reached the page already, as the turn's closing text in the record.
            self._index.update(conv_id, updated_at=self._wall())
            live.partial = ""
            live.last_used = self._now()
            self._clear_approvals(conv_id, live)
            live.framework_writes.clear()
            self._set_state(conv_id, "idle")
            if self._index.get(conv_id).kind == "discover":
                self._spawn_task(self._finish_discover_control(conv_id, link()))
            elif not failed:
                self._next_input(conv_id)
                if self._queue.next(conv_id) is None:
                    self._spawn_task(self._request_title(conv_id, live))

        def on_limits(live: Live, limits: dict) -> None:
            live.limits = {**(live.limits or {}), **limits}
            self._broadcast(conv_id, "limits", live.limits)

        def on_title(live: Live, title: str) -> None:
            live.native_title = title
            if live.first_completed and live.title_request is not None:
                self._apply_title(conv_id, live, title)

        def on_state(live: Live, state: str) -> None:
            if live.state not in BUSY:
                return
            if state in BUSY:
                self._set_state(conv_id, state)
            elif state == "idle" and live.state == "compacting":
                self._set_state(conv_id, "running")
            # Native status can arrive before the terminal RPC response. Only on_turn_end
            # releases the turn; otherwise a second prompt could overwrite a live request.

        return LinkEvents(on_delta=current(on_delta), on_entry=current(lambda live, e:
                          self._on_system(conv_id, live, e) if e["type"] == "system" else self._show(conv_id, live, e)),
                          on_usage=current(on_usage), on_permission=current(on_permission),
                          on_turn_end=current(on_turn_end),
                          on_exit=lambda expected, err: self._on_exit(conv_id, link(), expected, err),
                          on_retry=current(lambda _live, text: self._set_retry(conv_id, text)),
                          on_limits=current(on_limits), on_title=current(on_title),
                          on_notice=current(lambda _live, text: self._notice(conv_id, text)),
                          on_state=current(on_state))

    async def _answer_link(self, conv_id: str, link: OpencodeLink | GrokLink | CodexLink, request_id: str) -> None:
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
        live.framework_writes.clear()
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
        completed_discover_turn = (agent == "grok" and conv is not None and conv.kind == "discover"
                                   and live.result_seen)
        expected = expected or completed_discover_turn
        live.proc = None
        self._clear_approvals(conv_id, live)
        live.framework_writes.clear()
        live.partial = ""
        live.pending_cut = None  # an edit whose process died unwritten is lost: the next resume sees the old tip
        self._set_state(conv_id, "dormant")
        if completed_discover_turn:
            self._next_input(conv_id)
            if live.title_from is not None:
                self._spawn_task(self._request_title(conv_id, live))
        else:
            # An expected transport close is not proof that the current round succeeded.
            # Intentional stops and idle reaping detach first and never reach this branch.
            self._pause_inputs(conv_id)
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
            if delta.get("text"):
                self._confirm_input(conv_id)
            live.partial += delta["text"]
            self._broadcast(conv_id, "delta", {"text": delta["text"]})

    def _on_chat_message(self, conv_id: str, live: Live, msg: dict) -> None:
        if self._agent(conv_id) == "grok":
            msg = self._app_log(conv_id).message(msg)  # grok keeps no transcript the app can read
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
                self._on_tool_result(conv_id, live, item)
                self._broadcast(conv_id, "tool_result", item)
                continue
            if item["kind"] == "text":
                live.partial = ""
            elif item["kind"] == "notice" and item["text"] == "已停止":
                live.interrupted = True
            self._broadcast(conv_id, "item", item)
            if item["kind"] == "tool":
                if not msg.get("parent_tool_use_id"):
                    self._confirm_input(conv_id)
                self._on_tool(conv_id, item)

    def _apply_native_marker(self, conv_id: str, native_session: str) -> Conversation:
        """A durable AppLog marker owns the active native session even if its index cannot save."""
        conv = self._index.get(conv_id)
        field = {"codex": "codex_thread", "grok": "grok_session", "opencode": "opencode_session"}[conv.agent]
        if getattr(conv, field) == native_session:
            return conv
        changes = {field: native_session}
        try:
            return self._index.update(conv_id, **changes)
        except OSError as error:
            self._pause_inputs(conv_id, error=error)
            return replace(conv, **changes)

    def _on_system(self, conv_id: str, live: Live, msg: dict) -> None:
        if msg.get("subtype") == "status":
            if msg.get("status") == "compacting":
                self._set_state(conv_id, "compacting")
            elif live.state == "compacting":  # a stray status outside a turn must not wedge "running"
                self._set_state(conv_id, "running")
        elif msg.get("subtype") == "compact_boundary":
            if msg.get("native_session"):
                self._apply_native_marker(conv_id, msg["native_session"])
            live.context_tokens = None
            self._broadcast(conv_id, "item", compact_item(msg["uuid"], msg.get("compact_metadata") or msg.get("compactMetadata") or {}))
            self._broadcast(conv_id, "context", self._context(conv_id))
        elif msg.get("subtype") == "api_retry":
            # Nothing else shows while claude waits on the API, for minutes when it stalls.
            self._set_retry(conv_id, retry_text(msg))

    def _on_result(self, conv_id: str, live: Live, msg: dict) -> None:
        conv = self._index.get(conv_id)
        failure = _result_failure(msg, conv.agent)
        if not failure and not live.interrupted:
            self._confirm_input(conv_id)
        if failure or live.interrupted:
            self._pause_inputs(conv_id)
        elif conv.kind != "discover":
            self._next_input(conv_id)
        if failure is not None and not live.interrupted:
            self._notice(conv_id, failure)
        if conv.agent == "grok":
            live.result_seen = True  # even an error result: grok's exit after it is the normal end of the turn
            cost = msg.get("total_cost_usd")
            changes = ({"cost_usd": (conv.cost_usd or 0.0) + cost}
                       if isinstance(cost, (int, float)) and not isinstance(cost, bool) else {})
        else:
            changes = self._claude_window(conv, msg)
        conv = self._index.update(conv_id, updated_at=self._wall(), **changes)
        live.partial = ""
        live.last_used = self._now()
        self._clear_approvals(conv_id, live)  # an interrupted turn whose cancel never arrived
        live.framework_writes.clear()
        # A discover turn ends when grok exits (`_on_exit`): until then it still writes its session, so a
        # second resume, an eviction or a reap must not start.
        if conv.agent != "grok" or conv.kind != "discover":
            self._set_state(conv_id, "idle")
        self._broadcast(conv_id, "context", self._context(conv_id))
        if "cost_usd" in changes:
            self._broadcast(conv_id, "conv", self._summary(conv))
        if failure is None and not live.interrupted:
            live.first_completed = True
        if conv.kind != "discover" and live.title_from is not None:
            self._spawn_task(self._request_title(conv_id, live))

    @staticmethod
    def _claude_window(conv: Conversation, msg: dict) -> dict:
        # The result may list helper models too; the window that matters is the conversation's own model's.
        usage = msg.get("modelUsage") or {}
        own = [u for name, u in usage.items() if name.startswith(conv.model)]
        window = (own[0] if own else next(iter(usage.values()), {})).get("contextWindow")
        return {"context_window": window} if window else {}

    async def _request_title(self, conv_id: str, live: Live) -> None:
        if self._closing:
            return
        text = live.title_from
        if (text is None or not live.first_completed or live.state in BUSY
                or self._queue.next(conv_id) is not None or self._closing):
            return
        live.title_from = None
        live.title_request = f"title-{uuid.uuid4().hex[:8]}"
        request_id = live.title_request
        conv = self._conv(conv_id)
        temporary = None
        try:
            if conv.agent == "claude":
                if live.proc is not None:
                    await live.proc.generate_title(request_id, text)
                return
            if live.native_title:
                self._apply_title(conv_id, live, live.native_title)
                return
            if conv.kind == "discover":
                temporary = self._discover_control(conv)
                await temporary.start()
                title = await temporary.generate_title(text)
            elif live.proc is not None:
                title = await live.proc.generate_title(text)
            else:
                return
            if live.title_request == request_id and title:
                self._apply_title(conv_id, live, title)
        except (OSError, CodexSetupError, GrokSetupError, OpencodeSetupError) as error:
            log.info("chat %s: title generation failed: %s", conv_id, error)
        finally:
            if temporary is not None:
                await temporary.close()

    def _apply_title(self, conv_id: str, live: Live, title: str) -> None:
        if not title.strip() or (live.title_from is None and live.title_request is None):
            return
        live.title_from = live.title_request = None
        conv = self._index.update(conv_id, title=make_title(title))
        self._broadcast(conv_id, "conv", self._summary(conv))

    def _discover_control(self, conv: Conversation) -> GrokLink:
        cfg = self._require_grok()
        link = GrokLink(cfg, conv, self._grok_env, self._app_log(conv.id),
                        self._link_events(conv.id, lambda: link), cfg.workdir)
        return link

    async def _finish_discover_control(self, conv_id: str, link: GrokLink) -> None:
        live = self._live.get(conv_id)
        if live is None:
            return
        async with live.lock:
            if live.proc is link and live.state == "idle":
                await self._stop(conv_id)
        self._next_input(conv_id)
        if self._queue.next(conv_id) is None and not self._closing:
            self._spawn_task(self._request_title(conv_id, live))

    async def _spawn_discover_control(self, conv: Conversation, live: Live) -> None:
        generation = live.generation
        self._checkpoint(conv.id, live, generation)
        if live.proc is not None and live.proc.alive:
            await self._stop(conv.id)
        self._checkpoint(conv.id, live, generation)
        link = self._discover_control(conv)
        try:
            await self._start_owned(conv.id, live, generation, link, link.start)
        except (GrokSetupError, OSError) as error:
            await link.close()
            raise Conflict(f"无法恢复 Grok 对话：{error}") from error
        live.proc = link
        live.result_seen = False
        self._set_state(conv.id, "idle")

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
            if path is None:
                return None
            approval = new_approval(request_id, request, path, root)
            native_diff = (request.get("input") or {}).get("_codex_diff")
            if self._agent(conv_id) == "codex" and isinstance(native_diff, str):
                approval = replace(approval, diff=native_diff)
            return approval
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
        """Remember a possible framework write until its tool result confirms success."""
        conv = self._index.get(conv_id)
        if conv is None or conv.kind != "research" or conv.ticker is not None or item["name"] not in ("Write", "Edit"):
            return
        inputs = item["input"]
        if inputs.get("operation") == "delete":
            return
        ticker = framework_ticker(str(inputs.get("move_path") or inputs.get("file_path", "")),
                                  self._launch.repo_root)
        if ticker is not None:
            self._live[conv_id].framework_writes[item["id"]] = ticker

    def _on_tool_result(self, conv_id: str, live: Live, item: dict) -> None:
        ticker = live.framework_writes.pop(item["id"], None)
        if ticker is None or item["is_error"]:
            return
        conv = self._index.get(conv_id)
        if conv is not None and conv.kind == "research" and conv.ticker is None:
            conv = self._index.update(conv_id, ticker=ticker)
            live.framework_writes.clear()
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
            if state in BUSY and live.state not in BUSY:
                live.interrupted = False
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
        if conv.agent == "codex":
            labels = {m.id: m.label for m in self._codex_models}
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

    def _require_codex(self) -> CodexConfig:
        if self._codex is None:
            raise Invalid("Codex 未配置")
        return self._codex

    def _app_log(self, conv_id: str) -> AppLog:
        if conv_id not in self._app_logs:
            self._app_logs[conv_id] = AppLog(self._transcript(conv_id))
        return self._app_logs[conv_id]

    def _broadcast_entry(self, conv_id: str, entry: dict) -> None:
        for item in message_items(entry):
            self._broadcast(conv_id, "item", item)

    def _transcript(self, conv_id: str) -> Path:
        agent = self._agent(conv_id)
        if agent == "grok":
            return self._require_grok().logs / f"{conv_id}.jsonl"
        if agent == "opencode":
            return self._require_opencode().logs / f"{conv_id}.jsonl"
        if agent == "codex":
            return self._require_codex().logs / f"{conv_id}.jsonl"
        return self._projects / f"{conv_id}.jsonl"

    def _visible(self, conv: Conversation) -> bool:
        return not conv.archived and (conv.id in self._live or self._transcript(conv.id).exists())

    def _check_not_archived(self, conv_id: str, live: Live) -> None:
        """Under the chat's lock: an archive that ran while we waited for it has dropped `live`."""
        self._check_open()
        conv = self._index.get(conv_id)
        if live.archiving or self._live.get(conv_id) is not live or conv is None or conv.archived:
            raise NotFound(conv_id)

    def _check_open(self) -> None:
        if self._closing:
            raise Conflict("对话服务已关闭，请重新连接后发送")

    def _conv(self, conv_id: str, *, repair: bool = True) -> Conversation:
        conv = self._index.get(conv_id)
        if conv is None or not self._visible(conv):
            raise NotFound(conv_id)
        if repair and conv.agent != "claude":
            self._reconcile_inputs(conv_id)
            path = self._transcript(conv_id)
            entries = transcript.read_entries(path) if path.exists() else []
            branch = transcript.current_branch(entries)
            marker = next((entry for entry in reversed(branch) if entry.get("native_session")), None)
            if marker is not None:
                conv = self._apply_native_marker(conv_id, marker["native_session"])
        return conv
