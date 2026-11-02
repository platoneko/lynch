"""JSONL Codex app-server protocol adapted to the app's transcript and live event stream."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.approvals import APPROVABLE_SKILL, AUTO_DENY_MESSAGE
from lynch_reader.chat.codex import (SETUP_TIMEOUT_S, CodexConfig, CodexModel, codex_argv, command_allowed,
                                    file_scope, native_file_changes, parse_models, system_prompt)
from lynch_reader.chat.index import Conversation
from lynch_reader.chat.events import LinkEvents
from lynch_reader.chat.process import ChatProcess

log = logging.getLogger(__name__)


class CodexSetupError(Exception):
    pass


class _Rpc:
    def __init__(self, cfg: CodexConfig, env: dict[str, str], on_message: Callable[[dict], None],
                 on_exit: Callable[[bool, str], None], timeout: float) -> None:
        self._message, self._exit, self._timeout = on_message, on_exit, timeout
        self._calls: dict[int, asyncio.Future] = {}
        self._serial = 0
        self.proc = ChatProcess(codex_argv(cfg), env, cfg.repo_root, self._on_message, self._on_exit)
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        await self.proc.start()
        await self.call("initialize", {"clientInfo": {"name": "lynch_reader", "version": "0.1.0"}})
        await self.proc.send_json({"method": "initialized", "params": {}})
        account = await self.call("account/read", {"refreshToken": False})
        if account.get("requiresOpenaiAuth") and not account.get("account"):
            raise CodexSetupError("Codex 尚未登录，请在终端运行 codex login 后重试")

    async def call(self, method: str, params: dict) -> dict:
        self._serial += 1
        rpc_id = self._serial
        future = asyncio.get_running_loop().create_future()
        self._calls[rpc_id] = future
        try:
            await self.proc.send_json({"id": rpc_id, "method": method, "params": params})
            return await asyncio.wait_for(future, self._timeout)
        except TimeoutError as e:
            raise CodexSetupError(f"Codex 没有响应（{method}）") from e
        finally:
            self._calls.pop(rpc_id, None)

    def reply(self, rpc_id: int | str, result: dict | None = None, *, error: str | None = None) -> None:
        async def send() -> None:
            try:
                await self.proc.send_json({"id": rpc_id, **({"error": {"code": -32601, "message": error}}
                                                          if error else {"result": result or {}})})
            except OSError:
                pass  # the process's exit notification already reports the failure
        task = asyncio.create_task(send())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        await self.proc.close()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _on_message(self, msg: dict) -> None:
        if msg.get("method") is not None:
            self._message(msg)
            return
        future = self._calls.get(msg.get("id"))
        if future is None or future.done():
            return
        error = msg.get("error")
        if error:
            future.set_exception(CodexSetupError(str(error.get("message") or "Codex 请求失败")))
        else:
            future.set_result(msg.get("result") or {})

    def _on_exit(self, _proc: ChatProcess, expected: bool, stderr: str) -> None:
        for future in self._calls.values():
            if not future.done():
                future.set_exception(CodexSetupError(stderr or "Codex 进程退出"))
        self._exit(expected, stderr)


async def discover_models(cfg: CodexConfig, env: dict[str, str]) -> list[CodexModel]:
    """A short-lived connection; does not create a conversation or generate model output."""
    rpc: _Rpc
    rpc = _Rpc(cfg, env, lambda m: rpc.reply(m["id"], error="Unsupported request") if "id" in m else None,
               lambda *_: None, SETUP_TIMEOUT_S)
    try:
        await rpc.start()
        rows, seen, cursor = [], set(), None
        while True:
            page = await rpc.call("model/list", {"limit": 100, "includeHidden": False, "cursor": cursor})
            rows.extend(page.get("data") or [])
            cursor = page.get("nextCursor")
            if not cursor:
                return parse_models(rows)
            if cursor in seen:
                raise CodexSetupError("Codex 模型清单分页重复，请检查 Codex 版本")
            seen.add(cursor)
    except (OSError, ValueError, TypeError, KeyError) as e:
        raise CodexSetupError(f"无法读取 Codex 模型：{e}") from e
    finally:
        await rpc.close()


class CodexLink:
    def __init__(self, cfg: CodexConfig, conv: Conversation, env: dict[str, str], log: AppLog,
                 events: LinkEvents, *, setup_timeout_s: float = SETUP_TIMEOUT_S) -> None:
        self._cfg, self._conv, self._log, self._events = cfg, conv, log, events
        self._rpc = _Rpc(cfg, env, self._on_message, self._on_exit, setup_timeout_s)
        self._thread: str | None = None
        self._turn: str | None = None
        self._active = False
        self._starting = False
        self._cancelled_start = False
        self._deferred: list[dict] = []
        self._items: dict[str, dict] = {}
        self._texts: dict[str, str] = {}
        self._done: set[str] = set()
        self._pending = ""
        self._used: int | None = None
        self._asks: dict[str, tuple[int | str, set[str]]] = {}
        self._closed_turn = True

    @property
    def alive(self) -> bool:
        return self._rpc.proc.alive

    async def start(self) -> str:
        try:
            await self._rpc.start()
            # Read-only makes every native patch pass through the app's path checks. The untrusted policy
            # also asks for non-read-only commands; the app applies the same Bash guardrails as other agents.
            # These exact values are supported by the tested local 0.166.0 protocol schema.
            params = {"cwd": str(self._cfg.repo_root), "model": self._conv.model,
                      "approvalPolicy": "untrusted", "approvalsReviewer": "user", "sandbox": "read-only",
                      "developerInstructions": system_prompt(self._conv),
                      "config": {"model_reasoning_effort": self._conv.effort}}
            previous = self._conv.codex_thread
            result = await self._rpc.call("thread/resume" if previous else "thread/start",
                                          {**params, **({"threadId": previous} if previous else {})})
            self._thread = result["thread"]["id"]
            return self._thread
        except BaseException as e:
            await self._rpc.close()
            if isinstance(e, (CodexSetupError, asyncio.CancelledError)):
                raise
            raise CodexSetupError(f"无法启动 Codex：{e}") from e

    async def prompt(self, text: str) -> None:
        self._events.on_entry(self._log.user(text))
        self._active, self._closed_turn = True, False
        self._starting, self._cancelled_start = True, False
        self._deferred.clear()
        self._turn = None
        self._done.clear()
        try:
            result = await self._rpc.call("turn/start", {"threadId": self._thread,
                "input": [{"type": "text", "text": text}], "model": self._conv.model, "effort": self._conv.effort,
                # Queries need the network on new and resumed threads; native patches still require approval.
                "sandboxPolicy": {"type": "readOnly", "networkAccess": True}})
            if self._active:
                self._turn = result["turn"]["id"]
                self._starting = False
                deferred, self._deferred = self._deferred, []
                for message in deferred:
                    self._on_message(message)
        except BaseException as e:
            if self._cancelled_start:
                return  # interrupt() already stopped this process and recorded the user's stop
            if self._active:
                self._end_turn(None, str(e))
            # No reply does not mean the native turn never started. Close the uncertain connection before
            # the app can offer another send; the stored thread can be resumed on the next process.
            await self._rpc.close()
            raise

    async def interrupt(self) -> None:
        if not self._active:
            return
        if not self._turn:
            self._cancelled_start = True
            self._end_turn("cancelled", None)
            await self._rpc.close()
            return
        try:
            await self._rpc.call("turn/interrupt", {"threadId": self._thread, "turnId": self._turn})
        except (CodexSetupError, OSError):
            if self._active:
                self._end_turn("cancelled", None)
            await self._rpc.close()

    async def answer(self, request_id: str, allow: bool) -> None:
        ask = self._asks.pop(request_id, None)
        if ask is None:
            return
        rpc_id, remaining = ask
        remaining.discard(request_id)
        if not allow:
            for key in list(remaining):
                self._asks.pop(key, None)
            self._rpc.reply(rpc_id, {"decision": "decline"})
        elif not remaining:
            self._rpc.reply(rpc_id, {"decision": "accept"})

    async def close(self) -> None:
        await self._rpc.close()

    def _on_message(self, message: dict) -> None:
        method, p = message["method"], message.get("params") or {}
        if self._starting and p.get("threadId") == self._thread:
            # Bind notifications to the ID returned by turn/start, even if stdout drains notifications
            # before the coroutine awaiting that response resumes. Old lifecycle events use turn.id.
            self._deferred.append(message)
            return
        if "id" in message:
            self._on_request(message["id"], method, p)
            return
        if p.get("threadId") != self._thread or not self._active:
            return
        event_turn = p.get("turnId") or ((p.get("turn") or {}).get("id") if method.startswith("turn/") else None)
        if self._turn and event_turn and event_turn != self._turn:
            return
        if method == "turn/started":
            self._turn = (p.get("turn") or {}).get("id")
        elif method == "item/agentMessage/delta":
            item_id, delta = p.get("itemId"), p.get("delta") or ""
            if item_id not in self._done and delta:
                self._texts[item_id] = self._texts.get(item_id, "") + delta
                self._events.on_delta(delta)
        elif method in ("item/started", "item/completed"):
            item = p.get("item") or {}
            self._on_item(item, method == "item/completed")
        elif method == "thread/tokenUsage/updated":
            usage = p.get("tokenUsage") or {}
            used = (usage.get("last") or {}).get("totalTokens")
            if isinstance(used, int):
                self._used = used
                self._events.on_usage(used, usage.get("modelContextWindow"), None)
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            error = turn.get("error") or {}
            self._end_turn("cancelled" if turn.get("status") == "interrupted" else turn.get("status"),
                           str(error.get("message")) if error.get("message") else None)

    def _on_request(self, rpc_id: int | str, method: str, p: dict) -> None:
        if (p.get("threadId") != self._thread or not self._active
                or (self._turn and p.get("turnId") and p["turnId"] != self._turn)):
            self._rpc.reply(rpc_id, error="Request is not for this active conversation")
            return
        if method == "item/commandExecution/requestApproval":
            decision = "accept" if command_allowed(p.get("command")) else "decline"
            self._rpc.reply(rpc_id, {"decision": decision})
        elif method == "item/fileChange/requestApproval":
            item = self._items.get(p.get("itemId"), {})
            changes = item.get("changes") or []
            scope = file_scope(self._cfg.repo_root, changes)
            if scope != "ask":
                self._rpc.reply(rpc_id, {"decision": "accept" if scope == "allow" else "decline"})
                return
            skill = (self._cfg.repo_root / APPROVABLE_SKILL).resolve()
            to_ask = []
            for n, change in enumerate(native_file_changes(self._cfg.repo_root, changes) or []):
                path = next((p for p in (change.path, change.move_path)
                             if p is not None and p.is_relative_to(skill)), None)
                if path is not None:
                    to_ask.append((f"{rpc_id}:{n}", n, path, change))
            remaining = {request_id for request_id, _, _, _ in to_ask}
            for request_id, n, path, change in to_ask:
                self._asks[request_id] = (rpc_id, remaining)
                diff = change.diff
                if change.move_path is not None:
                    diff = f"操作：{change.operation}\n来源：{change.path}\n目标：{change.move_path}\n\n{diff}"
                self._events.on_permission(request_id, f"{item['id']}:{n}", "Write",
                                           {"file_path": str(path), "content": "", "_codex_diff": diff})
        else:
            self._rpc.reply(rpc_id, error="Unsupported request; ask the user in the conversation instead")

    def _on_item(self, item: dict, completed: bool) -> None:
        item_id, kind = item.get("id"), item.get("type")
        if not item_id or item_id in self._done:
            return
        if kind == "agentMessage":
            if completed:
                self._flush_text()
                self._pending = item.get("text") or self._texts.get(item_id, "")
                self._texts.pop(item_id, None)
                self._done.add(item_id)
            return
        if kind not in ("commandExecution", "fileChange", "webSearch", "mcpToolCall", "collabAgentToolCall"):
            return
        if item_id not in self._items:
            self._flush_text()
            self._write_tools(item)
        self._items[item_id] = item
        if completed:
            self._finish_tools(item)
            self._items.pop(item_id, None)
            self._done.add(item_id)

    def _tool_calls(self, item: dict) -> list[tuple[str, str, dict]]:
        item_id, kind = item["id"], item["type"]
        if kind == "fileChange":
            calls = []
            for n, change in enumerate(native_file_changes(self._cfg.repo_root, item.get("changes")) or []):
                inputs = {"file_path": str(change.path), "diff": change.diff, "operation": change.operation}
                if change.move_path is not None:
                    inputs["move_path"] = str(change.move_path)
                calls.append((f"{item_id}:{n}", "Edit", inputs))
            return calls
        if kind == "commandExecution":
            return [(item_id, "Bash", {"command": item.get("command") or ""})]
        if kind == "webSearch":
            return [(item_id, "WebSearch", {"query": item.get("query") or "", "action": item.get("action")})]
        if kind == "collabAgentToolCall":
            return [(item_id, "Agent", {"prompt": item.get("prompt") or "", "tool": item.get("tool"),
                                        "senderThreadId": item.get("senderThreadId"),
                                        "receiverThreadIds": item.get("receiverThreadIds") or []})]
        return [(item_id, f"mcp__{item.get('server')}__{item.get('tool')}", item.get("arguments") or {})]

    def _write_tools(self, item: dict) -> None:
        for call_id, name, inputs in self._tool_calls(item):
            self._write("assistant", [{"type": "tool_use", "id": call_id, "name": name, "input": inputs}])
        self._closed_turn = False

    def _finish_tools(self, item: dict, interrupted: bool = False) -> None:
        status = item.get("status")
        failed = interrupted or status in ("failed", "declined") or item.get("exitCode") not in (None, 0)
        output = ("已中断" if interrupted else AUTO_DENY_MESSAGE if status == "declined"
                  else item.get("aggregatedOutput") or item.get("error") or item.get("result") or "")
        if item.get("type") == "collabAgentToolCall" and not interrupted and not output:
            output = json.dumps(item.get("agentsStates") or {}, ensure_ascii=False)
        for call_id, _, _ in self._tool_calls(item):
            self._write("user", [{"type": "tool_result", "tool_use_id": call_id, "content": str(output), "is_error": failed}])

    def _write(self, role: str, content: list, usage: bool = False) -> None:
        message = {"role": role, "content": content}
        if usage and self._used is not None:
            message["usage"] = {"input_tokens": self._used}
        self._events.on_entry(self._log.message({"type": role, "message": message}))

    def _flush_text(self, usage: bool = False) -> None:
        if self._pending:
            self._write("assistant", [{"type": "text", "text": self._pending}], usage)
            self._pending = ""
            self._closed_turn = True

    def _end_turn(self, stop: str | None, error: str | None, notify: bool = True) -> None:
        for text in self._texts.values():
            self._pending += text
        self._texts.clear()
        self._flush_text(usage=True)
        for item in self._items.values():
            self._finish_tools(item, interrupted=True)
        self._items.clear()
        self._asks.clear()
        self._starting = False
        self._deferred.clear()
        self._active, self._turn = False, None
        if stop == "cancelled":
            self._events.on_entry(self._log.interrupted())
        elif error or (notify and not self._closed_turn):
            self._write("assistant", [{"type": "text", "text": f"Codex 本轮出错：{error}" if error else "（Codex 结束了本轮）"}], True)
        if notify:
            self._events.on_turn_end(stop, error)

    def _on_exit(self, expected: bool, stderr: str) -> None:
        if self._active:
            self._end_turn(None, None, notify=False)
        self._events.on_exit(expected, stderr)
