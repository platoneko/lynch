"""Chats: one Claude Code process per active conversation, its state, and fan-out to the app's event streams.

Everything here runs on the server's event loop; nothing blocks it except small file reads.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from lynch_reader.chat import transcript
from lynch_reader.chat.config import (CHAT_EFFORTS, CHAT_MODELS, DEFAULT_EFFORT, DEFAULT_MODEL, IDLE_TIMEOUT_S,
                                      MAX_PROCESSES, LaunchConfig, launch_argv, launch_env)
from lynch_reader.chat.index import ChatIndex, Conversation, make_title
from lynch_reader.chat.items import compact_item, context_tokens, message_items, notice_item
from lynch_reader.chat.process import ChatProcess

BUSY = ("running", "compacting")
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
        # All entries, not just the branch: parallel tool results hang off the branch (Task 4 fix).
        hist = transcript.history(transcript.current_branch(entries), entries)
        items = list(hist.items)
        if hist.unfinished and live.state == "dormant":
            items.append({"kind": "notice", "key": "interrupted", "text": "上次回答被中断"})
        tokens = live.context_tokens if live.context_tokens is not None else hist.context_tokens
        return {"conv": self._summary(conv), "items": items, "partial": live.partial, "usage": self.usage,
                "context": {"tokens": tokens, "window": conv.context_window}}

    # ---- commands

    async def create(self, kind: str, ticker: str | None, text: str, model: str, effort: str) -> Conversation:
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
        conv = Conversation(id=str(uuid.uuid4()), kind=kind, ticker=ticker if kind == "ticker" else None,
                            title=make_title(text), model=model, effort=effort, created_at=now, updated_at=now)
        self._index.put(conv)
        self._live[conv.id] = Live()  # visible in the list before its transcript exists
        await self.send(conv.id, text)
        return conv

    async def send(self, conv_id: str, text: str) -> None:
        conv = self._conv(conv_id)
        if not text.strip():
            raise Invalid("empty message")
        live = self._live.setdefault(conv_id, Live())
        if live.proc is None or not live.proc.alive:
            await self._spawn(conv)
        await self._write_user(conv_id, text)

    async def interrupt(self, conv_id: str) -> None:
        self._conv(conv_id)
        live = self._live.get(conv_id)
        if live and live.proc and live.proc.alive and live.state in BUSY:
            await live.proc.interrupt()

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
                           on_message=lambda msg: self._on_message(conv.id, msg),
                           on_exit=lambda p, expected, err: self._on_exit(conv.id, p, expected, err))
        try:
            await proc.start()
        except OSError as e:
            raise Conflict(f"无法启动 Claude：{e}") from e
        live.proc = proc
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
        live.partial = ""
        self._set_state(conv_id, "dormant")

    async def _make_room(self) -> None:
        alive = [(cid, live) for cid, live in self._live.items() if live.proc and live.proc.alive]
        excess = len(alive) + 1 - self._max_processes
        idle = sorted((live.last_used, cid) for cid, live in alive if live.state == "idle")
        for _, cid in idle[:max(0, excess)]:
            await self._stop(cid)

    def _on_exit(self, conv_id: str, proc: ChatProcess, expected: bool, stderr_tail: str) -> None:
        live = self._live.get(conv_id)
        if live is None or live.proc is not proc:
            return  # replaced by an edit or already stopped
        was_busy = live.state in BUSY
        live.proc = None
        live.partial = ""
        self._set_state(conv_id, "dormant")
        if not expected:
            text = "Claude 进程意外退出" + ("，本轮中断" if was_busy else "")
            self._notice(conv_id, f"{text}：{stderr_tail}" if stderr_tail else text)

    # ---- stream messages

    def _on_message(self, conv_id: str, msg: dict) -> None:
        live = self._live.get(conv_id)
        if live is None:
            return
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
            self._set_state(conv_id, "compacting" if msg.get("status") == "compacting" else "running")
        elif msg.get("subtype") == "compact_boundary":
            live.context_tokens = None
            self._broadcast(conv_id, "item", compact_item(msg["uuid"], msg.get("compact_metadata") or {}))
            self._broadcast(conv_id, "context", self._context(conv_id))

    def _on_result(self, conv_id: str, live: Live, msg: dict) -> None:
        window = next(iter((msg.get("modelUsage") or {}).values()), {}).get("contextWindow")
        self._index.update(conv_id, updated_at=self._wall(),
                           **({"context_window": window} if window else {}))
        live.partial = ""
        live.last_used = self._now()
        self._set_state(conv_id, "idle")
        self._broadcast(conv_id, "context", self._context(conv_id))

    def _on_rate_limit(self, conv_id: str, info: dict) -> None:
        windows = info.get("unifiedWindows") or {}
        self.usage = {"five_hour": (windows.get("five_hour") or {}).get("utilization"),
                      "seven_day": (windows.get("seven_day") or {}).get("utilization"),
                      "status": info.get("status"), "resets_at": info.get("resetsAt")}
        for cid in self._live:
            self._broadcast(cid, "usage", self.usage)

    def _on_tool(self, conv_id: str, item: dict) -> None:
        """Task 7 attaches a research conversation to the ticker it lands."""

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
        task.add_done_callback(self._tasks.discard)

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

    def _conv(self, conv_id: str) -> Conversation:
        conv = self._index.get(conv_id)
        if conv is None or not self._visible(conv):
            raise NotFound(conv_id)
        return conv
