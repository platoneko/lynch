"""One `opencode acp` process for one chat: ACP (JSON-RPC 2.0 over stdio) in, the app's transcript and events out.

opencode keeps no transcript the app can read, so the link writes each turn to the chat's AppLog in Claude Code's
shape (user / assistant text / tool_use / tool_result), and the service shows those entries as it shows Claude's.
Message shapes were verified against opencode 1.18.25 on 2026-10-18 (chat/opencode.py).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.index import Conversation
from lynch_reader.chat.opencode import (OPENCODE_MODE, SETUP_TIMEOUT_S, OpencodeConfig, claude_tool, mcp_servers,
                                        opencode_argv)
from lynch_reader.chat.process import ChatProcess

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
INTERRUPTED_OUTPUT = "已中断"  # a tool call the turn ended under, which opencode never finished


class OpencodeSetupError(Exception):
    pass


@dataclass
class LinkEvents:
    on_delta: Callable[[str], None]  # streamed reply text
    on_entry: Callable[[dict], None]  # an AppLog entry just appended
    on_usage: Callable[[int | None, int | None, float | None], None]  # context tokens, window, session cost USD
    on_permission: Callable[[str, str, str, dict], None]  # request id, tool call id, Claude tool name, its input
    on_turn_end: Callable[[str | None, str | None], None]  # stopReason, or None and the prompt's error text
    on_exit: Callable[[bool, str], None]  # expected, stderr tail


class OpencodeLink:
    def __init__(self, cfg: OpencodeConfig, conv: Conversation, env: dict[str, str], log: AppLog,
                 events: LinkEvents, *, setup_timeout_s: float = SETUP_TIMEOUT_S) -> None:
        self._cfg, self._conv, self._log, self._events = cfg, conv, log, events
        self._setup_timeout_s = setup_timeout_s
        self._proc = ChatProcess(opencode_argv(cfg), env, cfg.repo_root, on_message=self._on_message,
                                 on_exit=lambda _p, expected, err: self._on_exit(expected, err))
        self._session: str | None = None
        self._next_id = 0
        self._calls: dict[int, asyncio.Future] = {}  # setup requests awaiting their response
        self._prompt_id: int | None = None  # the running turn's session/prompt request
        # Per running tool call: its tool name (the first `tool_call`'s title) and input (it arrives on a later
        # update; the final one carries none).
        self._tools: dict[str, dict] = {}
        self._text, self._message_id = "", None  # the reply streaming since the last tool call
        self._used: int | None = None  # context tokens from this turn's usage_update
        self._asks: dict[str, tuple[int | str, list]] = {}  # permission requests: app id → (JSON-RPC id, options)
        self._tasks: set[asyncio.Task] = set()

    @property
    def alive(self) -> bool:
        return self._proc.alive

    async def start(self) -> str:
        """Starts the process and its session: a new one, or the chat's earlier one resumed without replay.
        Returns the session id; on any failure the process is gone and OpencodeSetupError says why."""
        try:
            await self._proc.start()
            await self._call("initialize", {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
            where = {"cwd": str(self._cfg.repo_root), "mcpServers": mcp_servers(self._cfg.repo_root)}
            if self._conv.opencode_session:
                session = self._conv.opencode_session
                result = await self._call("session/resume", {"sessionId": session, **where})
            else:
                result = await self._call("session/new", where)
                session = result["sessionId"]
            self._session = session
            # The user's own opencode defaults decide what a session starts in; the chat's choice wins.
            current = {o.get("id"): o.get("currentValue") for o in result.get("configOptions") or []}
            for config_id, value in (("mode", OPENCODE_MODE), ("model", self._conv.model)):
                if current.get(config_id) != value:
                    await self._call("session/set_config_option",
                                     {"sessionId": session, "configId": config_id, "value": value})
            return session
        except (OpencodeSetupError, OSError, KeyError, TypeError) as e:
            await self._proc.close()
            raise e if isinstance(e, OpencodeSetupError) else OpencodeSetupError(str(e)) from e

    async def prompt(self, text: str) -> None:
        """Sends one user message; the turn's end arrives as on_turn_end. opencode never echoes the message, so
        the app records it."""
        self._events.on_entry(self._log.user(text))
        self._prompt_id = self._new_id()
        await self._proc.send_json({"jsonrpc": "2.0", "id": self._prompt_id, "method": "session/prompt",
                                    "params": {"sessionId": self._session,
                                               "prompt": [{"type": "text", "text": text}]}})

    async def interrupt(self) -> None:
        """The turn then ends `cancelled`; a permission request still open needs no answer."""
        await self._proc.send_json({"jsonrpc": "2.0", "method": "session/cancel",
                                    "params": {"sessionId": self._session}})

    async def answer(self, request_id: str, allow: bool) -> None:
        ask = self._asks.pop(request_id, None)
        if ask is None:
            return  # the turn ended meanwhile
        rpc_id, options = ask
        kind = "allow_once" if allow else "reject_once"
        option = next((o["optionId"] for o in options if o.get("kind") == kind), kind)
        await self._proc.send_json({"jsonrpc": "2.0", "id": rpc_id,
                                    "result": {"outcome": {"outcome": "selected", "optionId": option}}})

    async def close(self) -> None:
        await self._proc.close()

    # ---- JSON-RPC

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _call(self, method: str, params: dict) -> dict:
        rpc_id = self._new_id()
        future = asyncio.get_running_loop().create_future()
        self._calls[rpc_id] = future
        await self._proc.send_json({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, self._setup_timeout_s)
        except TimeoutError as e:
            raise OpencodeSetupError(f"opencode 没有响应（{method}）") from e
        finally:
            self._calls.pop(rpc_id, None)

    def _on_message(self, msg: dict) -> None:
        method, rpc_id = msg.get("method"), msg.get("id")
        if method is None:
            self._on_response(rpc_id, msg)
        elif rpc_id is not None:
            self._on_request(rpc_id, method, msg.get("params") or {})
        elif method == "session/update":
            params = msg.get("params") or {}
            if params.get("sessionId") == self._session:
                self._on_update(params.get("update") or {})

    def _on_response(self, rpc_id: object, msg: dict) -> None:
        error = msg.get("error")
        if rpc_id is not None and rpc_id == self._prompt_id:
            if error:
                self._end_turn(None, str(error.get("message") or error))
            else:
                self._end_turn((msg.get("result") or {}).get("stopReason"), None)
            return
        future = self._calls.get(rpc_id)
        if future is None or future.done():
            return
        if error:
            future.set_exception(OpencodeSetupError(str(error.get("message") or error)))
        else:
            future.set_result(msg.get("result") or {})

    def _on_request(self, rpc_id: int | str, method: str, params: dict) -> None:
        if method != "session/request_permission":  # we offer no fs or terminal capability
            self._send_soon({"jsonrpc": "2.0", "id": rpc_id,
                             "error": {"code": -32601, "message": f"{method} is not supported"}})
            return
        call = params.get("toolCall") or {}
        call_id = str(call.get("toolCallId", ""))
        # The request's own rawInput is a diff of the whole file; the tool's input came on an earlier update.
        tracked = self._tools.get(call_id, {})
        name, tool_input = claude_tool(tracked.get("name") or str(call.get("title") or ""),
                                       tracked.get("input") or call.get("rawInput") or {})
        request_id = str(rpc_id)
        self._asks[request_id] = (rpc_id, params.get("options") or [])
        self._events.on_permission(request_id, call_id, name, tool_input)

    # ---- session updates → transcript and events

    def _on_update(self, u: dict) -> None:
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = (u.get("content") or {}).get("text") or ""
            if u.get("messageId") != self._message_id:
                self._flush_text()
                self._message_id = u.get("messageId")
            self._text += text
            if text:
                self._events.on_delta(text)
        elif kind in ("tool_call", "tool_call_update"):
            self._on_tool_update(kind, u)
        elif kind == "usage_update" and u.get("used"):  # a cancelled or failed turn reports 0: no reading
            self._used = u["used"]
            self._events.on_usage(u["used"], u.get("size"), (u.get("cost") or {}).get("amount"))
        # agent_thought_chunk is dropped like Claude's thinking; available_commands_update is not shown

    def _on_tool_update(self, kind: str, u: dict) -> None:
        call_id = str(u.get("toolCallId", ""))
        if kind == "tool_call":
            self._flush_text()  # the text before a tool call is its own entry, as in Claude's transcript
            self._tools[call_id] = {"name": u.get("title") or "tool", "input": {}}
        tracked = self._tools.setdefault(call_id, {"name": u.get("title") or "tool", "input": {}})
        if u.get("rawInput"):
            tracked["input"] = u["rawInput"]
        status = u.get("status")
        if status in ("completed", "failed"):
            raw = u.get("rawOutput") or {}
            output = raw.get("error") or raw.get("output") or _content_text(u.get("content"))
            self._finish_tool(call_id, str(output or ""), failed=status == "failed")

    def _finish_tool(self, call_id: str, output: str, failed: bool) -> None:
        tracked = self._tools.pop(call_id)
        name, tool_input = claude_tool(tracked["name"], tracked["input"])
        self._events.on_entry(self._log.message({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]}}))
        self._events.on_entry(self._log.message({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": output, "is_error": failed}]}}))

    def _flush_text(self, usage: dict | None = None) -> None:
        if not self._text:
            return
        message = {"role": "assistant", "content": [{"type": "text", "text": self._text}], **(usage or {})}
        self._text = ""
        self._events.on_entry(self._log.message({"type": "assistant", "message": message}))

    def _end_turn(self, stop: str | None, error: str | None, notify: bool = True) -> None:
        self._prompt_id = None
        # The context count rides on the turn's last text, so a reload after a restart still knows it.
        self._flush_text({"usage": {"input_tokens": self._used}} if self._used else None)
        self._message_id = None
        for call_id in list(self._tools):
            self._finish_tool(call_id, INTERRUPTED_OUTPUT, failed=True)
        self._asks.clear()
        if stop == "cancelled":
            self._events.on_entry(self._log.interrupted())
        if notify:
            self._events.on_turn_end(stop, error)

    def _on_exit(self, expected: bool, stderr_tail: str) -> None:
        if self._prompt_id is not None:  # what streamed so far stays in the record; the exit itself is the news
            self._end_turn(None, None, notify=False)
        for future in self._calls.values():
            if not future.done():
                future.set_exception(OpencodeSetupError(stderr_tail or "opencode 进程退出"))
        self._events.on_exit(expected, stderr_tail)

    def _send_soon(self, obj: dict) -> None:
        async def send() -> None:
            try:
                await self._proc.send_json(obj)
            except OSError as e:  # the process died: its exit is reported on its own
                log.info("opencode: reply not sent: %s", e)
        task = asyncio.get_running_loop().create_task(send())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _content_text(content: object) -> str:
    """Text of an ACP tool content list ([{type: content, content: {type: text, text}}, …])."""
    parts = []
    for c in content if isinstance(content, list) else []:
        inner = c.get("content") if isinstance(c, dict) else None
        if isinstance(inner, dict) and inner.get("type") == "text":
            parts.append(str(inner.get("text", "")))
    return "\n".join(parts)
