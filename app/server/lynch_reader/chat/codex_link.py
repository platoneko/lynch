"""JSONL Codex app-server protocol adapted to the app's transcript and live event stream."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from copy import deepcopy

from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.approvals import APPROVABLE_SKILL, AUTO_DENY_MESSAGE
from lynch_reader.chat.codex import (SETUP_TIMEOUT_S, CodexConfig, CodexModel, codex_argv, command_allowed,
                                    file_scope, native_file_changes, parse_models, system_prompt)
from lynch_reader.chat.index import Conversation
from lynch_reader.chat.events import LinkEvents
from lynch_reader.chat.mcp import codex_wisburg_config
from lynch_reader.chat.process import ChatProcess
from lynch_reader.chat.transcript import current_branch

log = logging.getLogger(__name__)


class CodexSetupError(Exception):
    pass


def _merge_sparse(previous: dict, update: dict) -> dict:
    """Native null fields mean unavailable, rather than clearing a known quota window."""
    merged = deepcopy(previous)
    for key, value in update.items():
        if value is None:
            continue
        merged[key] = (_merge_sparse(merged.get(key) if isinstance(merged.get(key), dict) else {}, value)
                       if isinstance(value, dict) else deepcopy(value))
    return merged


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
        await self.call("initialize", {"clientInfo": {"name": "lynch_reader", "version": "0.1.0"},
                                       "capabilities": {"experimentalApi": True}})
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
        self._env = dict(env)
        self._rpc = _Rpc(cfg, self._env, self._on_message, self._on_exit, setup_timeout_s)
        self._thread: str | None = None
        self._turn: str | None = None
        self._active = False
        self._starting = False
        self._cancelled_start = False
        self._deferred: list[dict] = []
        self._items: dict[str, dict] = {}
        self._texts: dict[str, str] = {}
        self._done: set[str] = set()
        self._finished_turns: set[str] = set()
        self._pending = ""
        self._used: int | None = None
        self._asks: dict[str, tuple[int | str, set[str]]] = {}
        self._children: set[str] = set()
        self._child_turns: dict[str, str] = {}
        self._child_items: dict[tuple[str, str], dict] = {}
        self._child_parents: dict[str, str] = {}
        self._child_done: set[tuple[str, str]] = set()
        self._finished_child_turns: set[tuple[str, str]] = set()
        self._last_turn: str | None = None
        self._retrying = False
        self._closed_turn = True
        self._manual_compact = False
        self._compact_override = False
        self._thread_params: dict = {}
        self._limits: dict = {"rateLimits": None}
        self._limits_ready = False

    @property
    def alive(self) -> bool:
        return self._rpc.proc.alive

    async def start(self) -> str:
        try:
            mcp_config = codex_wisburg_config(self._cfg.repo_root, self._env)
            await self._rpc.start()
            # Like Claude's Bash tools, approved scripts need to write caches and git metadata. On 0.166.0,
            # untrusted still asks for native patches with full access; the app checks every patch's scope.
            params = {"cwd": str(self._cfg.repo_root), "model": self._conv.model,
                      "approvalPolicy": "untrusted", "approvalsReviewer": "user", "sandbox": "danger-full-access",
                      "developerInstructions": system_prompt(self._conv),
                      "config": {"model_reasoning_effort": self._conv.effort, **mcp_config}}
            self._thread_params = params
            previous = self._conv.codex_thread
            result = await self._rpc.call("thread/resume" if previous else "thread/start",
                                          {**params, **({"threadId": previous} if previous else {})})
            self._thread = result["thread"]["id"]
            # Cache before returning: service callbacks cannot publish until it attaches this link.
            # A provider may omit the account API; quota failure must not prevent research.
            try:
                limits = await asyncio.wait_for(self._rpc.call("account/rateLimits/read", {}),
                                                min(self._rpc._timeout, 2.0))
                self._merge_limits(limits)
            except (CodexSetupError, OSError, TimeoutError):
                pass
            return self._thread
        except BaseException as e:
            await self._rpc.close()
            if isinstance(e, (CodexSetupError, asyncio.CancelledError)):
                raise
            raise CodexSetupError(f"无法启动 Codex：{e}") from e

    async def prompt(self, text: str) -> None:
        self._publish_initial_limits()
        await self._restore_compact_prompt()
        user = self._log.user(text)
        self._events.on_entry(user)
        await self._start_turn("turn/start", {"threadId": self._thread,
            "input": [{"type": "text", "text": text}], "model": self._conv.model, "effort": self._conv.effort,
            # Reapply both settings on resumed threads, including threads originally created read-only.
            "approvalPolicy": "untrusted", "sandboxPolicy": {"type": "dangerFullAccess"}}, user_uuid=user["uuid"])

    async def rewind(self, message_uuid: str) -> str:
        """Prepare a native prefix fork; the service commits the app branch afterwards."""
        if self._active:
            raise CodexSetupError("请等本轮结束后再编辑消息")
        await self._restore_compact_prompt()
        branch = current_branch(self._log.entries())
        users = [entry for entry in branch if entry.get("type") == "user"
                 and isinstance((entry.get("message") or {}).get("content"), str)
                 and not entry["message"]["content"].startswith("[Request interrupted")]
        target = next((entry for entry in users if entry.get("uuid") == message_uuid), None)
        if target is None:
            raise CodexSetupError("这条消息不在当前对话分支中")
        native_id = self._log.native_id(message_uuid)
        if not native_id:
            turns = await self._history()
            native = [(turn["id"], "".join(block.get("text", "") for item in turn.get("items", [])
                       if item.get("type") == "userMessage" for block in item.get("content", [])
                       if block.get("type") == "text")) for turn in turns
                      if any(item.get("type") == "userMessage" for item in turn.get("items", []))]
            texts = [entry["message"]["content"] for entry in users]
            matches = [start for start in range(len(native) - len(texts) + 1)
                       if [text for _, text in native[start:start + len(texts)]] == texts]
            if len(matches) != 1:
                raise CodexSetupError("无法唯一匹配旧聊天的原生轮次，请保留原分支后重试")
            native_id = native[matches[0] + users.index(target)][0]
        result = await self._rpc.call("thread/fork", {"threadId": self._thread, "beforeTurnId": native_id,
            "cwd": str(self._cfg.repo_root), "model": self._conv.model, "approvalPolicy": "untrusted",
            "approvalsReviewer": "user", "sandbox": "danger-full-access", "developerInstructions": system_prompt(self._conv)})
        thread = result["thread"]["id"]
        self._thread, self._last_turn = thread, None
        self._children.clear()
        self._child_turns.clear()
        self._child_items.clear()
        self._child_parents.clear()
        return thread

    async def _history(self) -> list[dict]:
        result = await self._rpc.call("thread/read", {"threadId": self._thread, "includeTurns": True})
        thread = result["thread"]
        if thread.get("historyMode") != "paginated":
            return thread.get("turns") or []
        turns, seen, cursor = [], set(), None
        while True:
            page = await self._rpc.call("thread/turns/list", {"threadId": self._thread, "limit": 100,
                "itemsView": "full", "sortDirection": "asc", "cursor": cursor})
            turns.extend(page.get("data") or [])
            cursor = page.get("nextCursor")
            if not cursor:
                return turns
            if cursor in seen:
                raise CodexSetupError("Codex 历史分页重复，无法安全编辑")
            seen.add(cursor)

    async def generate_title(self, description: str) -> str | None:
        if self._active:
            return None
        turn = self._last_turn
        if not turn:
            turns = await self._history()
            turn = turns[-1]["id"] if turns else None
        if not turn:
            return None
        result = await self._rpc.call("thread/sample", {"threadId": self._thread, "expectedTurnId": turn,
            "prompt": "为下面的研究对话起一个简短中文标题，只输出标题，不用工具：\n" + description,
            "effort": "low", "disableMcp": True})
        title = result.get("text")
        return title.strip().strip('"')[:40] if isinstance(title, str) and title.strip() else None

    async def compact(self, hint: str = "") -> None:
        self._publish_initial_limits()
        await self._restore_compact_prompt()
        if hint.strip():
            # Resuming an already loaded thread ignores config overrides. Unsubscribe first;
            # this unloads native state without changing the durable conversation or files.
            self._active, self._starting, self._cancelled_start = True, True, False
            self._turn = None
            self._compact_override = True
            try:
                await self._reload_thread({**self._thread_params["config"], "compact_prompt":
                    "Summarize this conversation for another model to continue the same task. Preserve the user's "
                    "goals, decisions, facts, relevant tool results, pending work and constraints. Do not do new research. "
                    "Apply these additional retention instructions to the summary:\n" + hint.strip()})
            except BaseException:
                if self._cancelled_start:
                    return
                self._active, self._starting = False, False
                raise
            if self._cancelled_start:
                return
        self._manual_compact = True
        await self._start_turn("thread/compact/start", {"threadId": self._thread})

    async def _reload_thread(self, config: dict) -> None:
        await self._rpc.call("thread/unsubscribe", {"threadId": self._thread})
        result = await self._rpc.call("thread/resume", {**self._thread_params, "threadId": self._thread,
                                                     "config": config})
        if result["thread"]["id"] != self._thread:
            raise CodexSetupError("Codex 恢复了不同的线程，已停止继续发送")

    async def _restore_compact_prompt(self) -> None:
        if self._compact_override:
            await self._reload_thread(self._thread_params["config"])
            self._compact_override = False

    async def _start_turn(self, method: str, params: dict, *, user_uuid: str | None = None) -> None:
        self._active, self._closed_turn = True, False
        self._starting, self._cancelled_start = True, False
        self._deferred.clear()
        self._turn = None
        self._done.clear()
        try:
            result = await self._rpc.call(method, params)
            if self._active:
                # Manual compaction acknowledges with {}; its turn ID arrives in turn/started.
                self._turn = result["turn"]["id"] if method == "turn/start" else None
                if user_uuid and self._turn:
                    self._log.bind_native(user_uuid, self._turn)
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

    def _publish_initial_limits(self) -> None:
        if not self._limits_ready:
            self._limits_ready = True
            self._events.on_limits(deepcopy(self._limits))

    def _merge_limits(self, payload: dict) -> None:
        update = {key: value for key, value in payload.items() if key in (
            "accountId", "ordinaryUsageAllowed", "rateLimitResetCredits", "rateLimitUpsell",
            "rateLimits", "rateLimitsByLimitId")}
        snapshot = update.get("rateLimits")
        if isinstance(snapshot, dict) and snapshot.get("limitId"):
            bucket = snapshot["limitId"]
            buckets = update.get("rateLimitsByLimitId")
            update["rateLimitsByLimitId"] = _merge_sparse(buckets if isinstance(buckets, dict) else {},
                                                         {bucket: snapshot})
            current_bucket = (self._limits.get("rateLimits") or {}).get("limitId")
            if current_bucket and current_bucket != bucket:
                update.pop("rateLimits")  # A different metered bucket cannot overwrite the default window.
        self._limits = _merge_sparse(self._limits, update)
        if self._limits_ready or self._active:
            self._events.on_limits(deepcopy(self._limits))

    def _on_message(self, message: dict) -> None:
        method, p = message["method"], message.get("params") or {}
        if self._starting:
            # stdout can drain parent spawn events and child approvals before turn/start resumes.
            # Replay the whole connection in order after binding the returned parent turn ID.
            self._deferred.append(message)
            return
        if method == "thread/started" and "id" not in message:
            thread = p.get("thread") or {}
            parent = thread.get("parentThreadId")
            if thread.get("id") != self._thread and parent and (parent == self._thread or parent in self._children):
                self._children.add(thread["id"])
                self._child_parents.setdefault(thread["id"], "codex-agent:" + thread["id"])
            return
        if method == "account/rateLimits/updated" and "id" not in message:
            self._merge_limits(p)
            return
        if "id" in message:
            self._on_request(message["id"], method, p)
            return
        if p.get("threadId") in self._children:
            self._on_child_event(method, p)
            return
        if p.get("threadId") != self._thread or not self._active:
            return
        event_turn = p.get("turnId") or ((p.get("turn") or {}).get("id") if method.startswith("turn/") else None)
        if event_turn in self._finished_turns:
            return
        if self._manual_compact and self._turn is None and method != "turn/started":
            return  # wait for the new compaction turn rather than consuming an old turn's tail
        if self._turn and event_turn and event_turn != self._turn:
            return
        if method == "thread/status/changed":
            state = (p.get("status") or {}).get("type")
            if state in ("active", "idle"):
                self._events.on_state(("compacting" if self._manual_compact else "running")
                                      if state == "active" else "idle")
            return
        if method == "error":
            error = (p.get("error") or {}).get("message") or "Codex 请求失败"
            if p.get("willRetry"):
                self._retrying = True
                self._events.on_retry(str(error))
            else:
                self._clear_retry()
            return
        if method in ("item/started", "item/completed", "item/agentMessage/delta", "turn/completed"):
            self._clear_retry()
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

    def _on_child_event(self, method: str, p: dict) -> None:
        if not self._active:
            return
        thread = p["threadId"]
        turn = (p.get("turn") or {}).get("id") if method.startswith("turn/") else p.get("turnId")
        if (thread, turn) in self._finished_child_turns:
            return
        if method == "turn/started":
            self._child_turns[thread] = turn
            self._child_items = {key: item for key, item in self._child_items.items() if key[0] != thread}
            self._child_done = {key for key in self._child_done if key[0] != thread}
        if not turn or self._child_turns.get(thread) != turn:
            return
        if method == "turn/completed":
            self._finished_child_turns.add((thread, turn))
            for (item_thread, _), item in self._child_items.items():
                if item_thread == thread:
                    self._finish_tools({**item, "id": "codex:" + thread + ":" + item["id"]},
                        interrupted=True, parent=self._child_parents.get(thread, "codex-agent:" + thread))
            self._child_turns.pop(thread, None)
            self._child_items = {key: item for key, item in self._child_items.items() if key[0] != thread}
        elif method in ("item/started", "item/completed"):
            item = p.get("item") or {}
            item_id = item.get("id")
            key = (thread, item_id)
            if not item_id or key in self._child_done:
                return
            parent = self._child_parents.get(thread, "codex-agent:" + thread)
            display_item = {**item, "id": "codex:" + thread + ":" + item_id}
            self._track_child(display_item)
            if item.get("type") == "agentMessage" and method == "item/completed":
                self._write("assistant", [{"type": "text", "text": item.get("text") or ""}], parent=parent)
                self._child_done.add(key)
            elif item.get("type") in ("commandExecution", "fileChange", "webSearch", "mcpToolCall", "collabAgentToolCall"):
                if key not in self._child_items:
                    self._write_tools(display_item, parent=parent)
                self._child_items[key] = item
                if method == "item/completed":
                    self._finish_tools(display_item, parent=parent)
                    self._child_items.pop(key, None)
                    self._child_done.add(key)

    def _on_request(self, rpc_id: int | str, method: str, p: dict) -> None:
        thread = p.get("threadId")
        child = thread in self._children
        valid_turn = (bool(p.get("turnId")) and p["turnId"] == self._child_turns.get(thread)) if child else (
            thread == self._thread and (not self._turn or not p.get("turnId") or p["turnId"] == self._turn))
        if not self._active or not valid_turn or (not child and p.get("turnId") in self._finished_turns):
            self._rpc.reply(rpc_id, error="Request is not for this active conversation")
            return
        if method == "item/commandExecution/requestApproval":
            decision = "accept" if command_allowed(p.get("command")) else "decline"
            self._rpc.reply(rpc_id, {"decision": decision})
        elif method == "item/fileChange/requestApproval":
            item = (self._child_items.get((thread, p.get("itemId")), {}) if child
                    else self._items.get(p.get("itemId"), {}))
            changes = item.get("changes") or []
            scope = file_scope(self._cfg.repo_root, changes)
            if scope != "ask" or child:
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
        self._track_child(item)
        if kind == "contextCompaction":
            self._flush_text(usage=True)
            if completed:
                self._done.add(item_id)
                self._events.on_entry(self._log.compacted("manual" if self._manual_compact else "auto", self._used))
                self._used = None
                if self._manual_compact:
                    self._closed_turn = True
                else:
                    self._events.on_entry({"type": "system", "subtype": "status", "status": None})
            else:
                self._events.on_entry({"type": "system", "subtype": "status", "status": "compacting"})
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

    def _track_child(self, item: dict) -> None:
        # Current Codex reports spawned agents as parent items, without thread/started for the child.
        if item.get("type") == "subAgentActivity" and item.get("kind") == "started":
            children = [item.get("agentThreadId")]
        elif item.get("type") == "collabAgentToolCall":
            children = item.get("receiverThreadIds") or []
        else:
            return
        for child in children:
            # A child reporting back to this conversation names the parent as its receiver.
            if isinstance(child, str) and child and child != self._thread:
                self._children.add(child)
                self._child_parents[child] = item["id"]

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

    def _write_tools(self, item: dict, *, parent: str | None = None) -> None:
        for call_id, name, inputs in self._tool_calls(item):
            self._write("assistant", [{"type": "tool_use", "id": call_id, "name": name, "input": inputs}], parent=parent)
        if parent is None:
            self._closed_turn = False

    def _finish_tools(self, item: dict, interrupted: bool = False, *, parent: str | None = None) -> None:
        status = item.get("status")
        failed = interrupted or status in ("failed", "declined") or item.get("exitCode") not in (None, 0)
        output = ("已中断" if interrupted else AUTO_DENY_MESSAGE if status == "declined"
                  else item.get("aggregatedOutput") or item.get("error") or item.get("result") or "")
        if item.get("type") == "collabAgentToolCall" and not interrupted and not output:
            output = json.dumps(item.get("agentsStates") or {}, ensure_ascii=False)
        for call_id, _, _ in self._tool_calls(item):
            self._write("user", [{"type": "tool_result", "tool_use_id": call_id, "content": str(output), "is_error": failed}], parent=parent)

    def _write(self, role: str, content: list, usage: bool = False, *, parent: str | None = None) -> None:
        message = {"role": role, "content": content}
        if usage and self._used is not None:
            message["usage"] = {"input_tokens": self._used}
        self._events.on_entry(self._log.message({"type": role, "message": message,
            **({"parent_tool_use_id": parent} if parent else {})}))

    def _clear_retry(self) -> None:
        if self._retrying:
            self._retrying = False
            self._events.on_retry(None)

    def _flush_text(self, usage: bool = False) -> None:
        if self._pending:
            self._write("assistant", [{"type": "text", "text": self._pending}], usage)
            self._pending = ""
            self._closed_turn = True

    def _end_turn(self, stop: str | None, error: str | None, notify: bool = True) -> None:
        self._clear_retry()
        for text in self._texts.values():
            self._pending += text
        self._texts.clear()
        self._flush_text(usage=True)
        for item in self._items.values():
            self._finish_tools(item, interrupted=True)
        self._items.clear()
        for (thread, _), item in self._child_items.items():
            self._finish_tools({**item, "id": "codex:" + thread + ":" + item["id"]}, interrupted=True,
                               parent=self._child_parents.get(thread, "codex-agent:" + thread))
        self._child_items.clear()
        self._finished_child_turns.update(self._child_turns.items())
        self._child_turns.clear()
        self._asks.clear()
        self._starting = False
        self._deferred.clear()
        if self._turn:
            self._finished_turns.add(self._turn)
            self._last_turn = self._turn
        self._active, self._turn = False, None
        self._manual_compact = False
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
