"""One `grok agent stdio` process for one research/ticker chat: ACP in, the app's transcript and events out.

Grok keeps no app-readable transcript, so the link writes each turn to AppLog in Claude Code's shape. Message
shapes were verified against grok 1.0.70 on 2026-10-31 (spike §§3–7). Discover chats still use chat/grok.py `_Acp`.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.approvals import approvable_path
from lynch_reader.chat.grok import (ACP_TIMEOUT_S, GrokConfig, GrokSetupError, grok_tool, research_argv,
                                    research_prompt, write_research_rules)
from lynch_reader.chat.index import Conversation
from lynch_reader.chat.opencode import mcp_servers
from lynch_reader.chat.opencode_link import LinkEvents
from lynch_reader.chat.process import ChatProcess

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
INTERRUPTED_OUTPUT = "已中断"
TURN_ENDED_TEXT = "（Grok 结束了本轮）"
COST_TICKS_PER_USD = 10_000_000_000  # grok docs: costUsdTicks is 10¹⁰ ticks per USD


class _RpcError(GrokSetupError):
    """Grok answered a setup request with a JSON-RPC error."""


class GrokLink:
    def __init__(self, cfg: GrokConfig, conv: Conversation, env: dict[str, str], log: AppLog,
                 events: LinkEvents, repo_root: Path, *, setup_timeout_s: float = ACP_TIMEOUT_S) -> None:
        self._cfg, self._conv, self._log, self._events = cfg, conv, log, events
        self._repo_root = repo_root
        self._setup_timeout_s = setup_timeout_s
        self._proc = ChatProcess(research_argv(cfg), env, repo_root, on_message=self._on_message,
                                 on_exit=lambda _p, expected, err: self._on_exit(expected, err))
        self._session: str | None = None
        self._next_id = 0
        self._calls: dict[int, asyncio.Future] = {}
        self._prompt_id: int | None = None
        self._tools: dict[str, dict] = {}
        self._text, self._message_id = "", None
        self._used: int | None = None
        self._asks: dict[str, tuple[int | str, list]] = {}
        self._closed_turn = True
        self._tasks: set[asyncio.Task] = set()
        self.session_lost = False

    @property
    def alive(self) -> bool:
        return self._proc.alive

    async def start(self) -> str:
        write_research_rules(self._cfg, self._conv)
        try:
            await self._proc.start()
            await self._call("initialize", {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
            where = {"cwd": str(self._repo_root), "mcpServers": mcp_servers(self._repo_root),
                     "_meta": {"rules": research_prompt(self._conv)}}
            result = None
            if self._conv.grok_session:
                try:
                    result = await self._call("session/resume",
                                             {"sessionId": self._conv.grok_session, **where})
                    session = self._conv.grok_session
                except _RpcError as e:
                    log.warning("grok: session %s not resumed: %s", self._conv.grok_session, e)
                    self.session_lost = True
            if result is None:
                result = await self._call("session/new", where)
                session = result["sessionId"]
            self._session = session
            await self._call("session/set_model", {
                "sessionId": session, "modelId": self._conv.model,
                "_meta": {"contextWindow": self._conv.context_window or 500_000}})
            await self._call("session/set_config_option", {
                "sessionId": session, "configId": "reasoning_effort", "value": self._conv.effort})
            return session
        except Exception as e:
            await self._proc.close()
            raise e if isinstance(e, GrokSetupError) else GrokSetupError(f"{type(e).__name__}: {e}") from e

    async def prompt(self, text: str) -> None:
        self._events.on_entry(self._log.user(text))
        self._closed_turn = False
        self._prompt_id = self._new_id()
        await self._proc.send_json({"jsonrpc": "2.0", "id": self._prompt_id, "method": "session/prompt",
                                    "params": {"sessionId": self._session,
                                               "prompt": [{"type": "text", "text": text}]}})

    async def interrupt(self) -> None:
        await self._proc.send_json({"jsonrpc": "2.0", "method": "session/cancel",
                                    "params": {"sessionId": self._session}})

    async def answer(self, request_id: str, allow: bool) -> None:
        ask = self._asks.pop(request_id, None)
        if ask is None:
            return
        rpc_id, options = ask
        kind = "allow_once" if allow else "reject_once"
        option = next((o["optionId"] for o in options if o.get("kind") == kind), kind)
        await self._proc.send_json({"jsonrpc": "2.0", "id": rpc_id,
                                    "result": {"outcome": {"outcome": "selected", "optionId": option}}})

    async def close(self) -> None:
        await self._proc.close()

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
            raise GrokSetupError(f"grok 没有响应（{method}）") from e
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
                result = msg.get("result") or {}
                self._ingest_prompt_usage(result.get("_meta") or {})
                self._end_turn(result.get("stopReason"), None)
            return
        future = self._calls.get(rpc_id)
        if future is None or future.done():
            return
        if error:
            future.set_exception(_RpcError(str(error.get("message") or error)))
        else:
            future.set_result(msg.get("result") or {})

    def _on_request(self, rpc_id: int | str, method: str, params: dict) -> None:
        if method != "session/request_permission":
            self._send_soon({"jsonrpc": "2.0", "id": rpc_id,
                             "error": {"code": -32601, "message": f"{method} is not supported"}})
            return
        call = params.get("toolCall") or {}
        call_id = str(call.get("toolCallId", ""))
        tracked = self._tools.get(call_id, {})
        name, tool_input = grok_tool(tracked.get("name") or str(call.get("title") or ""),
                                     tracked.get("input") or call.get("rawInput") or {})
        tool_input = self._abs_paths(tool_input)
        options = params.get("options") or []
        request_id = str(rpc_id)
        if name == "Agent":  # spawn_subagent: spec allows it; dontAsk still asks because Agent is unrecognized
            self._asks[request_id] = (rpc_id, options)
            self._send_soon_answer(request_id, True)
            return
        if approvable_path(name, tool_input, self._repo_root) is None:
            self._asks[request_id] = (rpc_id, options)
            self._send_soon_answer(request_id, False)
            return
        self._asks[request_id] = (rpc_id, options)
        self._events.on_permission(request_id, call_id, name, tool_input)

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
        elif kind == "usage_update" and u.get("used"):
            self._used = u["used"]
            cost = (u.get("cost") or {}).get("amount")
            self._events.on_usage(u["used"], u.get("size"), cost)

    def _ingest_prompt_usage(self, meta: dict) -> None:
        if self._used:
            return
        used = meta.get("totalTokens") or (meta.get("usage") or {}).get("totalTokens")
        if not used:
            return
        self._used = int(used)
        ticks = (meta.get("usage") or {}).get("costUsdTicks")
        cost = (ticks / COST_TICKS_PER_USD) if ticks else None
        self._events.on_usage(self._used, self._conv.context_window, cost)

    def _on_tool_update(self, kind: str, u: dict) -> None:
        call_id = str(u.get("toolCallId", ""))
        if kind == "tool_call":
            self._flush_text()
            self._tools[call_id] = {"name": u.get("title") or "tool", "input": {}}
        tracked = self._tools.setdefault(call_id, {"name": u.get("title") or "tool", "input": {}})
        if u.get("title"):
            tracked["name"] = u["title"]
        if u.get("rawInput"):
            tracked["input"] = self._abs_paths(u["rawInput"])
        status = u.get("status")
        if status in ("completed", "failed"):
            raw = u.get("rawOutput") or {}
            output = raw.get("error") or raw.get("output") or _content_text(u.get("content"))
            self._finish_tool(call_id, str(output or ""), failed=status == "failed")

    def _finish_tool(self, call_id: str, output: str, failed: bool) -> None:
        tracked = self._tools.pop(call_id)
        name, tool_input = grok_tool(tracked["name"], tracked["input"])
        self._events.on_entry(self._log.message({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]}}))
        self._events.on_entry(self._log.message({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": output, "is_error": failed}]}}))
        self._closed_turn = False

    def _flush_text(self, usage: dict | None = None) -> None:
        if self._text:
            self._write_text(self._text, usage)
            self._text = ""

    def _write_text(self, text: str, usage: dict | None = None) -> None:
        message = {"role": "assistant", "content": [{"type": "text", "text": text}], **(usage or {})}
        self._events.on_entry(self._log.message({"type": "assistant", "message": message}))
        self._closed_turn = True

    def _end_turn(self, stop: str | None, error: str | None, notify: bool = True) -> None:
        self._prompt_id = None
        usage = {"usage": {"input_tokens": self._used}} if self._used else None
        self._flush_text(None if error or not notify else usage)
        self._message_id = None
        for call_id in list(self._tools):
            self._finish_tool(call_id, INTERRUPTED_OUTPUT, failed=True)
        self._asks.clear()
        if stop == "cancelled":
            self._events.on_entry(self._log.interrupted())
        elif notify and (error is not None or not self._closed_turn):
            self._write_text(f"Grok 本轮出错：{error}" if error is not None else TURN_ENDED_TEXT, usage)
        if notify:
            self._events.on_turn_end(stop, error)

    def _on_exit(self, expected: bool, stderr_tail: str) -> None:
        if self._prompt_id is not None:
            self._end_turn(None, None, notify=False)
        for future in self._calls.values():
            if not future.done():
                future.set_exception(GrokSetupError(stderr_tail or "grok 进程退出"))
        self._events.on_exit(expected, stderr_tail)

    def _send_soon_answer(self, request_id: str, allow: bool) -> None:
        async def go() -> None:
            try:
                await self.answer(request_id, allow)
            except OSError as e:
                log.info("grok: permission answer not sent: %s", e)
        self._spawn(go())

    def _send_soon(self, obj: dict) -> None:
        async def send() -> None:
            try:
                await self._proc.send_json(obj)
            except OSError as e:
                log.info("grok: reply not sent: %s", e)
        self._spawn(send())

    def _abs_paths(self, tool_input: dict) -> dict:
        """Grok file tools may send a path relative to cwd, which is the repo root (spike §3)."""
        inp = dict(tool_input)
        for key in ("file_path", "target_file"):
            raw = inp.get(key)
            if isinstance(raw, str) and raw and not Path(raw).is_absolute():
                inp[key] = str((self._repo_root / raw).resolve())
        return inp

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _content_text(content: object) -> str:
    parts = []
    for c in content if isinstance(content, list) else []:
        inner = c.get("content") if isinstance(c, dict) else None
        if isinstance(inner, dict) and inner.get("type") == "text":
            parts.append(str(inner.get("text", "")))
    return "\n".join(parts)
