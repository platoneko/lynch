"""Chats: one Claude Code process per active conversation, its state, and fan-out to the app's event streams.

Everything here runs on the server's event loop; nothing blocks it except small file reads.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from lynch_reader.chat import transcript
from lynch_reader.chat.config import (CHAT_EFFORTS, CHAT_MODELS, DEFAULT_EFFORT, DEFAULT_MODEL, IDLE_TIMEOUT_S,
                                      MAX_PROCESSES, LaunchConfig, launch_argv, launch_env)
from lynch_reader.chat.index import ChatIndex, Conversation, make_title
from lynch_reader.chat.items import compact_item, context_tokens, framework_ticker, message_items, notice_item
from lynch_reader.chat.process import ChatProcess

log = logging.getLogger(__name__)

BUSY = ("running", "compacting")
COMPACT_COMMAND = "/compact"
KINDS = ("research", "ticker")


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
    proc: ChatProcess | None = None
    last_used: float = 0.0
    partial: str = ""  # streamed text not yet closed by its full message
    context_tokens: int | None = None
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    # A new conversation's first message, until its first reply ends and claude is asked for a title;
    # then the id of that request, whose answer replaces the title unless the user renamed meanwhile.
    title_from: str | None = None
    title_request: str | None = None
    # (resume uuid, transcript entry count) of an edit whose new process has not written yet;
    # until it does, the file's newest message is still the edited-away branch.
    pending_cut: tuple[str, int] | None = None
    # Held by public commands that spawn or stop this chat's own process (send, edit, archive),
    # so two of them never race on `proc`. `_spawn` / `_stop` / `_make_room` never take it:
    # `_make_room` stops other chats' idle processes without their lock, so no lock-order deadlock.
    # Eviction (`_make_room`, `reap_idle`) skips a chat whose lock is held: a command is writing to it.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ChatService:
    def __init__(self, index: ChatIndex, launch: LaunchConfig, projects_dir: Path, base_env: Mapping[str, str], *,
                 monotonic: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 idle_timeout_s: float = IDLE_TIMEOUT_S, max_processes: int = MAX_PROCESSES) -> None:
        self._index = index
        self._launch = launch
        self._projects = projects_dir
        self._env = launch_env(base_env)
        self._now, self._wall = monotonic, wall
        self._idle_timeout_s, self._max_processes = idle_timeout_s, max_processes
        self._live: dict[str, Live] = {}
        self._tasks: set[asyncio.Task] = set()
        self.usage: dict | None = None

    # ---- queries

    def options(self) -> dict:
        return {"models": [{"id": m, "label": label} for m, label in CHAT_MODELS], "efforts": list(CHAT_EFFORTS),
                "default_model": DEFAULT_MODEL, "default_effort": DEFAULT_EFFORT}

    def list(self) -> dict:
        rows = [self._summary(c) for c in self._index.all() if self._visible(c)]
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        return {"chats": rows, "running_count": sum(r["state"] in BUSY for r in rows), "usage": self.usage}

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
        return {"conv": self._summary(conv), "items": items, "partial": live.partial, "usage": self.usage,
                "context": {"tokens": tokens, "window": conv.context_window}}

    # ---- commands

    async def create(self, kind: str, ticker: str | None, text: str, model: str, effort: str) -> Conversation:
        # The values come from a JSON body; a list or dict would break the membership tests below.
        if not all(isinstance(v, str) for v in (kind, text, model, effort)) or not isinstance(ticker, str | None):
            raise Invalid("fields must be strings")
        if kind not in KINDS:
            raise Invalid(f"unknown kind {kind!r}")
        if kind == "ticker" and not ticker:
            raise Invalid("a ticker conversation needs a ticker")
        if model not in {m for m, _ in CHAT_MODELS}:
            raise Invalid(f"unknown model {model!r}")
        if effort not in CHAT_EFFORTS:
            raise Invalid(f"unknown effort {effort!r}")
        if not text.strip():
            raise Invalid("empty message")
        now = self._wall()
        # The window arrives with the first turn's result; until then, the last one seen for this model.
        seen = [c for c in self._index.all() if c.model == model and c.context_window]
        window = max(seen, key=lambda c: c.updated_at).context_window if seen else None
        conv = Conversation(id=str(uuid.uuid4()), kind=kind, ticker=ticker if kind == "ticker" else None,
                            title=make_title(text), model=model, effort=effort, created_at=now, updated_at=now,
                            context_window=window)
        self._index.put(conv)
        self._live[conv.id] = Live(title_from=text)  # visible in the list before its transcript exists
        await self.send(conv.id, text)
        return conv

    async def send(self, conv_id: str, text: str) -> None:
        conv = self._conv(conv_id)
        if not text.strip():
            raise Invalid("empty message")
        live = self._live.setdefault(conv_id, Live())
        async with live.lock:
            self._check_not_archived(conv_id, live)
            if text.lstrip().startswith(COMPACT_COMMAND) and live.state in BUSY:
                raise Conflict("等这一轮结束再压缩")
            if live.proc is None or not live.proc.alive:
                await self._spawn(conv)
            await self._write_user(conv_id, text)

    async def interrupt(self, conv_id: str) -> None:
        self._conv(conv_id)
        live = self._live.get(conv_id)
        if live and live.proc and live.proc.alive and live.state in BUSY:
            try:
                await live.proc.interrupt()
            except (BrokenPipeError, ConnectionResetError) as e:  # it exited after the alive check
                raise Conflict("Claude 进程已退出，原因见对话里的提示") from e

    async def edit(self, conv_id: str, message_uuid: str, text: str) -> None:
        """Truncate the conversation before `message_uuid` and send `text` in its place. Files are not touched."""
        conv = self._conv(conv_id)
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

    async def _stop(self, conv_id: str) -> None:
        live = self._live.get(conv_id)
        if live is None:
            return
        proc, live.proc = live.proc, None  # detach first: its exit is then ignored as stale
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
        live.proc = None
        live.partial = ""
        live.pending_cut = None  # an edit whose process died unwritten is lost: the next resume sees the old tip
        self._set_state(conv_id, "dormant")
        if not expected:
            text = "Claude 进程意外退出" + ("，本轮中断" if was_busy else "")
            self._notice(conv_id, f"{text}：{stderr_tail}" if stderr_tail else text)

    # ---- stream messages

    def _on_message(self, conv_id: str, proc: ChatProcess, msg: dict) -> None:
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
        elif kind == "control_response":
            self._on_control_response(conv_id, live, msg.get("response") or {})

    def _on_stream_event(self, conv_id: str, live: Live, event: dict) -> None:
        self._mark_busy(conv_id)
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            live.partial += delta["text"]
            self._broadcast(conv_id, "delta", {"text": delta["text"]})

    def _on_chat_message(self, conv_id: str, live: Live, msg: dict) -> None:
        if msg["type"] == "assistant":
            self._mark_busy(conv_id)
            tokens = context_tokens(msg["message"])
            if tokens:
                live.context_tokens = tokens
                self._broadcast(conv_id, "context", self._context(conv_id))
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

    def _on_result(self, conv_id: str, live: Live, msg: dict) -> None:
        # The result may list helper models too; the window that matters is the conversation's own model's.
        usage = msg.get("modelUsage") or {}
        model = self._index.get(conv_id).model
        own = [u for name, u in usage.items() if name.startswith(model)]
        window = (own[0] if own else next(iter(usage.values()), {})).get("contextWindow")
        self._index.update(conv_id, updated_at=self._wall(),
                           **({"context_window": window} if window else {}))
        live.partial = ""
        live.last_used = self._now()
        self._set_state(conv_id, "idle")
        self._broadcast(conv_id, "context", self._context(conv_id))
        if live.title_from is not None:
            self._spawn_task(self._request_title(conv_id, live))

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

    def _on_rate_limit(self, conv_id: str, info: dict) -> None:
        windows = info.get("unifiedWindows") or {}
        self.usage = {"five_hour": (windows.get("five_hour") or {}).get("utilization"),
                      "seven_day": (windows.get("seven_day") or {}).get("utilization"),
                      "status": info.get("status"), "resets_at": info.get("resetsAt")}
        for cid in self._live:
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
        if live.state != state:
            live.state = state
            self._broadcast(conv_id, "state", {"state": state})

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
        return {**asdict(conv), "state": live.state if live else "dormant"}

    def _transcript(self, conv_id: str) -> Path:
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
