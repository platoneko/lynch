"""One `opencode acp` process for one chat: ACP (JSON-RPC 2.0 over stdio) in, the app's transcript and events out.

ACP supplies streamed turns and approvals; the authenticated loopback HTTP API supplies exact native message
boundaries, summaries, retry events and titles. AppLog keeps the app-visible transcript in Claude's shape.
The HTTP bridge and native fork/summary behavior were verified against opencode 1.19.5.
"""
from __future__ import annotations

import asyncio
import logging
import json
import secrets
import socket

from lynch_reader.chat.opencode_http import OpencodeHttp
from lynch_reader.chat.transcript import current_branch

from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.events import LinkEvents
from lynch_reader.chat.index import Conversation
from lynch_reader.chat.opencode import (OPENCODE_MODE, SETUP_TIMEOUT_S, OpencodeConfig, claude_tool, mcp_servers,
                                        opencode_argv)
from lynch_reader.chat.process import ChatProcess

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
INTERRUPTED_OUTPUT = "已中断"  # a tool call the turn ended under, which opencode never finished
TURN_ENDED_TEXT = "（opencode 结束了本轮）"


class OpencodeSetupError(Exception):
    pass


class _RpcError(OpencodeSetupError):
    """opencode answered a setup request with a JSON-RPC error (as opposed to no answer or an exit)."""


class OpencodeLink:
    def __init__(self, cfg: OpencodeConfig, conv: Conversation, env: dict[str, str], log: AppLog,
                 events: LinkEvents, *, setup_timeout_s: float = SETUP_TIMEOUT_S) -> None:
        self._cfg, self._conv, self._log, self._events = cfg, conv, log, events
        self._setup_timeout_s = setup_timeout_s
        self._env = env
        self._restarting = False
        self._closing = False
        self._native_task: asyncio.Task | None = None
        self._compacting = False
        self._compact_cancelled = False
        self._compact_message: str | None = None
        boundaries = {entry["uuid"] for entry in self._log.entries() if entry.get("subtype") == "compact_boundary"}
        self._completed_compactions = {entry["native_id"] for entry in self._log.entries()
                                       if entry.get("type") == "lynch_native" and entry.get("user_uuid") in boundaries}
        self._native_compacting = False
        self._title: str | None = None
        self._pending_user: dict | None = None
        self._before_messages: set[str] = set()
        self._proc = self._make_process()
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
        self._closed_turn = True  # the log's newest entry ends a turn (a reply text, or no turn yet)
        self._tasks: set[asyncio.Task] = set()
        self.session_lost = False  # compatibility with callers; missing sessions now fail instead of replacing them

    @property
    def alive(self) -> bool:
        return self._proc.alive

    async def start(self) -> str:
        """Resume the exact native session, or create a session for a new chat. Fail closed on lost history."""
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
            await self._configure_session(session, result)
            await self._sync_compactions()
            self._native_task = asyncio.create_task(self._watch_native())
            return session
        except BaseException as e:  # cancellation must also release the not-yet-attached process
            await self._proc.close()
            if not isinstance(e, Exception):
                raise
            raise e if isinstance(e, OpencodeSetupError) else OpencodeSetupError(f"{type(e).__name__}: {e}") from e

    async def prompt(self, text: str) -> None:
        """Sends one user message; the turn's end arrives as on_turn_end. opencode never echoes the message, so
        the app records it."""
        messages = await self._messages()
        self._before_messages = {m["info"]["id"] for m in messages}
        self._pending_user = self._log.user(text)
        self._events.on_entry(self._pending_user)
        self._used = None
        self._closed_turn = False
        self._prompt_id = self._new_id()
        await self._proc.send_json({"jsonrpc": "2.0", "id": self._prompt_id, "method": "session/prompt",
                                    "params": {"sessionId": self._session,
                                               "prompt": [{"type": "text", "text": text}]}})

    async def interrupt(self) -> None:
        """The turn then ends `cancelled`; a permission request still open needs no answer."""
        if self._compacting:
            self._compact_cancelled = True
            if self._restarting:
                return
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
        self._closing = True
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._stop_native()
        await self._proc.close()

    def _make_process(self, hint: str = "") -> ChatProcess:
        # The random password stays in this process's environment and the in-memory bridge only.
        password = secrets.token_urlsafe(32)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {**self._env, "OPENCODE_SERVER_PASSWORD": password, "OPENCODE_SERVER_USERNAME": "opencode"}
        if hint:
            overlay = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
            overlay.setdefault("agent", {}).setdefault("compaction", {})["prompt"] = (
                "Summarize the conversation faithfully for continued work. Preserve decisions, constraints, "
                "open tasks, exact identifiers and important facts. Give special attention to the user's "
                "preservation request below, without dropping the rest of the necessary context:\n" + hint)
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(overlay, ensure_ascii=False)
        self._http = OpencodeHttp(port, password, str(self._cfg.repo_root))
        return ChatProcess([*opencode_argv(self._cfg), "--hostname", "127.0.0.1", "--port", str(port)],
                           env, self._cfg.repo_root, on_message=self._on_message,
                           on_exit=lambda _p, expected, err: self._on_exit(expected, err))

    async def _stop_native(self) -> None:
        if self._native_task:
            self._native_task.cancel()
            await asyncio.gather(self._native_task, return_exceptions=True)
            self._native_task = None

    async def _configure_session(self, session: str, result: dict) -> None:
        current = {o.get("id"): o.get("currentValue") for o in result.get("configOptions") or []}
        for config_id, value in (("mode", OPENCODE_MODE), ("model", self._conv.model)):
            if current.get(config_id) != value:
                await self._call("session/set_config_option",
                                 {"sessionId": session, "configId": config_id, "value": value})

    async def _restart(self, hint: str = "") -> None:
        self._restarting = True
        try:
            await self._stop_native()
            await self._proc.close()
            self._proc = self._make_process(hint)
            await self._proc.start()
            await self._call("initialize", {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}})
            result = await self._call("session/resume", {"sessionId": self._session, "cwd": str(self._cfg.repo_root),
                                                         "mcpServers": mcp_servers(self._cfg.repo_root)})
            await self._configure_session(self._session, result)
            self._native_task = asyncio.create_task(self._watch_native())
        except BaseException:
            await self._proc.close()
            raise
        finally:
            self._restarting = False

    async def _messages(self) -> list[dict]:
        return await self._http.request("GET", f"/session/{self._session}/message")

    async def _bind_pending(self) -> None:
        if self._pending_user is None:
            return
        content = self._pending_user["message"]["content"]
        found = [m for m in await self._messages() if m["info"]["id"] not in self._before_messages
                 and m["info"]["role"] == "user" and _native_text(m) == content]
        if len(found) != 1:
            raise OpencodeSetupError("无法核实 opencode 已接收的消息位置；请保留本轮记录后重试")
        self._log.bind_native(self._pending_user["uuid"], found[0]["info"]["id"])
        self._pending_user = None

    async def rewind(self, message_uuid: str) -> str:
        if self._prompt_id is not None or self._compacting:
            raise OpencodeSetupError("请等待当前回答结束后再编辑")
        branch = current_branch(self._log.entries())
        entry = next((e for e in branch if e.get("uuid") == message_uuid), None)
        if entry is None or entry.get("type") != "user" or not isinstance(entry["message"]["content"], str):
            raise OpencodeSetupError("找不到要编辑的用户消息")
        messages = await self._messages()
        native_id = self._log.native_id(message_uuid)
        if not any(m["info"]["id"] == native_id for m in messages):
            matches = [m for m in messages if m["info"]["role"] == "user"
                       and _native_text(m) == entry["message"]["content"]]
            if len(matches) != 1:
                raise OpencodeSetupError("旧对话消息无法唯一匹配原生历史，未切换对话分支")
            native_id = matches[0]["info"]["id"]
        old = self._session
        try:
            fork = await self._http.request("POST", f"/session/{old}/fork", {"messageID": native_id})
            new = fork["id"]
            copied = await self._http.request("GET", f"/session/{new}/message")
            boundary = next(i for i, m in enumerate(messages) if m["info"]["id"] == native_id)
            if len(copied) != boundary:
                raise OpencodeSetupError("opencode 分叉边界与原始消息不一致")
            result = await self._call("session/resume", {"sessionId": new, "cwd": str(self._cfg.repo_root),
                                                         "mcpServers": mcp_servers(self._cfg.repo_root)})
            await self._configure_session(new, result)
        except Exception as error:
            raise OpencodeSetupError(str(error)) from error
        self._session = new
        self._used = None
        self._title = None
        mapping = {m["info"]["id"]: c["info"]["id"] for m, c in zip(messages[:boundary], copied)}
        self._completed_compactions = {mapping.get(mid, mid) for mid in self._completed_compactions}
        self._compact_message = None
        self._native_compacting = False
        for retained in branch:
            mapped = mapping.get(self._log.native_id(retained["uuid"]))
            if mapped:
                self._log.bind_native(retained["uuid"], mapped)
        return new

    async def compact(self, hint: str = "") -> None:
        if self._prompt_id is not None or self._compacting:
            raise OpencodeSetupError("请等待当前回答结束后再压缩")
        self._compacting = True
        self._compact_cancelled = False
        self._events.on_state("compacting")
        task = asyncio.create_task(self._run_compaction(hint))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_compaction(self, hint: str) -> None:
        error = None
        try:
            before = {m["info"]["id"] for m in await self._messages()}
            if hint:
                await self._restart(hint)
            if self._compact_cancelled:
                return
            provider, model = self._conv.model.split("/", 1)
            await self._http.request("POST", f"/session/{self._session}/summarize",
                                     {"providerID": provider, "modelID": model, "auto": False}, timeout=600)
            summaries = [m for m in await self._messages() if m["info"]["id"] not in before
                         and m["info"].get("summary") and m["info"].get("time", {}).get("completed")
                         and not m["info"].get("error")]
            if not summaries and not self._compact_cancelled:
                raise OpencodeSetupError("opencode 未完成原生摘要，原对话仍保留")
            if summaries:
                self._finish_compaction(summaries[-1]["info"]["parentID"], "manual")
        except asyncio.CancelledError:
            self._compact_cancelled = True
            raise
        except Exception as exc:
            error = str(exc)
        finally:
            if hint and not self._closing:
                try:
                    await self._restart()
                except Exception as exc:
                    error = f"压缩后恢复 opencode 失败：{exc}"
            self._compacting = False
            self._events.on_state("idle")
            self._events.on_turn_end("cancelled" if self._compact_cancelled else "end_turn", error)

    async def generate_title(self, description: str) -> str | None:
        info = await self._http.request("GET", f"/session/{self._session}")
        title = info.get("title") or self._title
        return title if title and not title.startswith("New session") else None

    async def _sync_compactions(self) -> None:
        messages = await self._messages()
        parts = {message["info"]["id"]: next((part for part in message.get("parts", [])
                 if part.get("type") == "compaction"), None) for message in messages}
        for message in messages:
            info = message["info"]
            parent = info.get("parentID")
            if info.get("summary") and info.get("time", {}).get("completed") and not info.get("error"):
                part = parts.get(parent)
                if part is not None:
                    self._finish_compaction(parent, "auto" if part.get("auto") else "manual")

    async def _watch_native(self) -> None:
        while self.alive:
            try:
                async for event in self._http.events():
                    self._on_native(event)
            except (OSError, ValueError, RuntimeError, asyncio.IncompleteReadError):
                if self.alive:
                    await asyncio.sleep(0.5)

    def _finish_compaction(self, native_id: str, trigger: str) -> None:
        if native_id in self._completed_compactions:
            return
        self._completed_compactions.add(native_id)
        boundary = self._log.compacted(trigger, self._used)
        self._log.bind_native(boundary["uuid"], native_id)
        self._events.on_entry(boundary)
        self._used = None

    def _on_native(self, event: dict) -> None:
        properties = event.get("properties") or {}
        part = properties.get("part") or {}
        info = properties.get("info") or {}
        session = properties.get("sessionID") or part.get("sessionID") or info.get("sessionID") or info.get("id")
        if session != self._session:
            return
        kind = event.get("type")
        if kind == "session.updated":
            title = info.get("title")
            if title and not title.startswith("New session"):
                self._title = title
                self._events.on_title(title)
        elif kind == "session.status":
            status = properties.get("status") or {}
            if status.get("type") == "retry":
                self._events.on_retry(str(status.get("message") or "opencode 正在重试"))
            elif status.get("type") in ("busy", "idle"):
                self._events.on_retry(None)
                if status["type"] == "idle" and self._native_compacting:
                    self._native_compacting = False
                    if not self._compacting:
                        self._events.on_state("running" if self._prompt_id else "idle")
        elif kind == "message.part.updated" and part.get("type") == "compaction":
            self._compact_message = part["messageID"]
            self._native_compacting = True
            self._auto_compact = bool(part.get("auto"))
            self._events.on_state("compacting")
        elif kind == "session.compacted" and self._compact_message:
            self._native_compacting = False
            self._finish_compaction(self._compact_message, "auto" if self._auto_compact else "manual")
            if not self._compacting:
                self._events.on_state("running" if self._prompt_id else "idle")

    async def _complete_turn(self, stop: str | None, error: str | None) -> None:
        try:
            await self._bind_pending()
        except Exception as exc:
            error = error or str(exc)
        self._end_turn(stop, error)

    # ---- JSON-RPC

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _call(self, method: str, params: dict) -> dict:
        rpc_id = self._new_id()
        future = asyncio.get_running_loop().create_future()
        self._calls[rpc_id] = future
        try:
            await self._proc.send_json({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
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
            stop = (msg.get("result") or {}).get("stopReason") if not error else None
            text = str(error.get("message") or error) if error else None
            task = asyncio.create_task(self._complete_turn(stop, text))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        future = self._calls.get(rpc_id)
        if future is None or future.done():
            return
        if error:
            future.set_exception(_RpcError(str(error.get("message") or error)))
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
            if self._compacting or self._native_compacting:
                return
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
        if kind == "tool_call" or u.get("rawInput"):
            name, tool_input = claude_tool(tracked["name"], tracked["input"])
            self._events.on_entry(self._log.message({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]}}))
        status = u.get("status")
        if status in ("completed", "failed"):
            raw = u.get("rawOutput") or {}
            output = raw.get("error") or raw.get("output") or _content_text(u.get("content"))
            self._finish_tool(call_id, str(output or ""), failed=status == "failed")

    def _finish_tool(self, call_id: str, output: str, failed: bool) -> None:
        self._tools.pop(call_id)
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
        # The context count rides on the turn's last text, so a reload after a restart still knows it.
        usage = {"usage": {"input_tokens": self._used}} if self._used else None
        self._flush_text(None if error or not notify else usage)
        self._message_id = None
        for call_id in list(self._tools):
            self._finish_tool(call_id, INTERRUPTED_OUTPUT, failed=True)
        self._asks.clear()
        if stop == "cancelled":
            self._events.on_entry(self._log.interrupted())
        elif notify and (error is not None or not self._closed_turn):
            # A turn that ends on an error or right after a refusal (opencode stops there) gets a closing line,
            # so the record does not read as cut off once the process is gone.
            self._write_text(f"opencode 本轮出错：{error}" if error is not None else TURN_ENDED_TEXT, usage)
        if notify:
            self._events.on_turn_end(stop, error)

    def _on_exit(self, expected: bool, stderr_tail: str) -> None:
        for future in self._calls.values():
            if not future.done():
                future.set_exception(OpencodeSetupError(stderr_tail or "opencode 进程退出"))
        if self._restarting:
            return
        if self._prompt_id is not None:  # what streamed so far stays in the record; the exit itself is the news
            self._end_turn(None, None, notify=False)
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


def _native_text(message: dict) -> str:
    return "".join(part.get("text", "") for part in message.get("parts", []) if part.get("type") == "text")
