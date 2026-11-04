"""One `grok agent stdio` process for one research/ticker chat: ACP in, the app's transcript and events out.

Grok keeps no app-readable transcript, so the link writes each turn to AppLog in Claude Code's shape. Message
shapes were verified against grok 1.0.70 on 2026-10-31 (spike §§3–7). Discover chats still use chat/grok.py `_Acp`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.approvals import approvable_path
from lynch_reader.chat.events import LinkEvents
from lynch_reader.chat.grok import (ACP_TIMEOUT_S, GrokConfig, GrokSetupError, grok_tool, research_argv,
                                    bootstrap_summary, new_session, research_prompt, summarize_conversation, USER_HEADING,
                                    write_research_rules)
from lynch_reader.chat.index import Conversation
from lynch_reader.chat.items import INTERRUPT_PREFIX
from lynch_reader.chat.opencode import mcp_servers
from lynch_reader.chat.process import ChatProcess
from lynch_reader.chat.transcript import current_branch

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
        self._env = env
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
        self._title = ""
        self._manual_compact = False
        self._compact_recorded = False
        self._replay_prompts: dict[int, str] = {}
        self._compact_task: asyncio.Task | None = None
        self._pending_user: tuple[str, str] | None = None
        self._needs_resume = False
        self._closing = False
        self._log_sync_failed = False
        self._log_abort_task: asyncio.Task | None = None
        self._log_abort_error = ""

    @property
    def alive(self) -> bool:
        return self._proc.alive

    async def start(self) -> str:
        if self._conv.kind != "discover":
            write_research_rules(self._cfg, self._conv)
        try:
            # The service may reuse its AppLog after a previous link closed on an uncertain
            # write. Never append through that cached cursor before reconciling disk again.
            try:
                self._log.refresh()
            except OSError as error:
                raise GrokSetupError("Grok 对话记录无法读取，暂不能恢复会话") from error
            await self._proc.start()
            await self._call("initialize", {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
            where = self._where()
            result = None
            if self._conv.grok_session:
                self._session = self._conv.grok_session
                try:
                    result = await self._call("session/resume",
                                             {"sessionId": self._conv.grok_session, **where})
                    session = self._conv.grok_session
                except _RpcError as e:
                    log.warning("grok: session %s not resumed: %s", self._conv.grok_session, e)
                    self.session_lost = True
                    raise GrokSetupError(f"Grok 原会话恢复失败，已保留原会话：{e}") from e
            if result is None:
                result = await self._call("session/new", where)
                session = result["sessionId"]
            self._session = session
            self._title = ((result.get("_meta") or {}).get("x.ai/sessionDetail") or {}).get("title") or ""
            await self._call("session/set_model", {
                "sessionId": session, "modelId": self._conv.model,
                "_meta": {"contextWindow": self._conv.context_window or 500_000}})
            await self._call("session/set_config_option", {
                "sessionId": session, "configId": "reasoning_effort", "value": self._conv.effort})
            return session
        except BaseException as e:
            await self._proc.close()
            if not isinstance(e, Exception):
                raise
            raise e if isinstance(e, GrokSetupError) else GrokSetupError(f"{type(e).__name__}: {e}") from e

    async def prompt(self, text: str) -> None:
        if self._log_sync_failed:
            raise GrokSetupError("压缩边界无法与磁盘核对，请重新打开对话后继续")
        if self._needs_resume:
            await self._call("session/resume", {"sessionId": self._session, **self._where()})
            self._needs_resume = False
        if self._log_sync_failed:
            raise GrokSetupError(self._log_abort_error)
        entry = self._log.user(text, native_pending=True)
        self._pending_user = (entry["uuid"], text)
        self._events.on_entry(entry)
        self._used = None
        self._closed_turn = False
        self._prompt_id = self._new_id()
        try:
            await self._proc.send_json({"jsonrpc": "2.0", "id": self._prompt_id, "method": "session/prompt",
                                    "params": {"sessionId": self._session,
                                               "prompt": [{"type": "text", "text": text}]}})
        except BaseException:
            self._prompt_id = None
            self._pending_user = None
            raise

    async def rewind(self, message_uuid: str) -> str:
        """Keep the old native branch, and rewind only the copied conversation."""
        if self._log_sync_failed:
            raise GrokSetupError(self._log_abort_error)
        native = self._log.native_id(message_uuid)
        if native is None or not native.isdecimal():
            self._replay_prompts.clear()
            await self._call("session/load", {"sessionId": self._session, **self._where()})
            if self._log_sync_failed:
                raise GrokSetupError(self._log_abort_error)
            branch = current_branch(self._log.entries())
            boundary = max((i for i, e in enumerate(branch) if e.get("subtype") == "compact_boundary"), default=-1)
            users = [e for e in branch[boundary + 1:] if e.get("type") == "user"
                     and isinstance(e.get("message", {}).get("content"), str)
                     and not e["message"]["content"].startswith(INTERRUPT_PREFIX)]
            prompts = sorted(self._replay_prompts.items())
            if boundary >= 0 and users:
                prompts = prompts[-len(users):]
            if len(users) != len(prompts) or any(not self._matches_native(e["message"]["content"], text)
                                               for e, (_, text) in zip(users, prompts)):
                raise GrokSetupError("旧消息与 Grok 原生历史无法准确对应，已保留原分支")
            for entry, (index, _) in zip(users, prompts):
                self._log.bind_native(entry["uuid"], str(index))
            native = self._log.native_id(message_uuid)
        if native is None:
            raise GrokSetupError("找不到这条消息对应的 Grok 原生位置")
        old = self._session
        fork = await self._call("_x.ai/session/fork", {"sourceSessionId": old,
                                "sourceCwd": str(self._repo_root), "newCwd": str(self._repo_root)})
        session = fork["newSessionId"]
        try:
            await self._call("session/resume", {"sessionId": session, **self._where()})
            result = await self._call("_x.ai/rewind/execute", {"sessionId": session,
                                      "targetPromptIndex": int(native), "mode": "conversation_only", "force": True})
            if result.get("success") is not True:
                raise GrokSetupError(str(result.get("error") or "Grok 没有完成历史回退"))
        except BaseException:
            await self._call("session/resume", {"sessionId": old, **self._where()})
            raise
        self._session = session
        return session

    async def compact(self, hint: str = "") -> None:
        if self._log_sync_failed:
            raise GrokSetupError("压缩边界无法与磁盘核对，请重新打开对话后继续")
        if self._compact_task is not None or self._prompt_id is not None:
            raise GrokSetupError("Grok 正在处理上一轮请求")
        self._manual_compact, self._compact_recorded = True, False
        self._events.on_state("compacting")
        self._compact_task = asyncio.create_task(self._run_compact(hint))
        # Start the owned coroutine before accepting an immediate stop/close, so its finally
        # always reports the one terminal event even when no model request has started.
        await asyncio.sleep(0)

    async def _run_compact(self, hint: str) -> None:
        stop, error = "end_turn", None
        try:
            if hint.strip():
                await self._compact_with_hint(hint)
            else:
                await self._call("_x.ai/compact_conversation", {"sessionId": self._session}, timeout_s=180)
                self._record_compact("manual", self._used)
        except asyncio.CancelledError:
            stop = "cancelled"
        except Exception as exc:
            error = str(exc)
            if not self._log_sync_failed:
                notice = "压缩已保存，但状态更新失败" if self._compact_recorded else "压缩失败"
                self._events.on_notice(f"{notice}：{error}")
        finally:
            self._manual_compact = False
            self._compact_task = None
            if not self._log_sync_failed:
                self._events.on_state("idle")
                self._events.on_turn_end(stop, error)

    async def _compact_with_hint(self, hint: str) -> str:
        source = await self._call("_x.ai/session/fork", {"sourceSessionId": self._session,
                                  "sourceCwd": str(self._repo_root), "newCwd": str(self._repo_root)})
        summary = await summarize_conversation(self._cfg, self._env,
                            replace(self._conv, grok_session=source["newSessionId"]), hint, self._repo_root)
        session = await new_session(replace(self._cfg, workdir=self._repo_root), self._env, self._conv.model,
                                    self._conv.context_window or 500_000)
        await bootstrap_summary(self._cfg, self._env, replace(self._conv, grok_session=session),
                                summary, self._repo_root)
        # Prepare has no authority to change the active native branch. The durable marker
        # is the commit point; readers can recover it even if a later callback/index fails.
        entry = self._append_compact_boundary("manual", self._used, session)
        self._session = session
        self._needs_resume = True
        self._publish_compact(entry)
        return session

    async def generate_title(self, description: str = "") -> str:
        if not self._title:
            result = await self._call("session/list", {"cwd": str(self._repo_root)})
            self._title = next((s.get("title") or "" for s in result.get("sessions", [])
                                if s.get("sessionId") == self._session), "")
        return self._title

    def _where(self) -> dict:
        where = {"cwd": str(self._repo_root), "mcpServers": []}
        if self._conv.kind != "discover":
            where.update(mcpServers=mcp_servers(self._repo_root),
                         _meta={"rules": research_prompt(self._conv)})
        return where

    def _matches_native(self, text: str, native: str) -> bool:
        if text == native:
            return True
        return self._conv.kind == "discover" and native.endswith(f"{USER_HEADING}\n{text}")

    def _record_compact(self, trigger: str, before: int | None) -> None:
        if self._log_sync_failed:
            return
        if self._manual_compact and self._compact_recorded:
            return
        entry = self._append_compact_boundary(trigger, before, self._session)
        self._publish_compact(entry)

    def _append_compact_boundary(self, trigger: str, before: int | None, session: str) -> dict:
        if self._log_sync_failed:
            raise GrokSetupError(self._log_abort_error)
        marker = str(uuid4())
        try:
            return self._log.compacted(trigger, before, uuid=marker, native_session=session, agent="grok")
        except Exception:
            # A write can have committed before reporting an error. Re-read this exact
            # marker and restore the cursor/torn-line state before any later append.
            try:
                entries = self._log.refresh()
            except Exception as error:
                message = "压缩边界写入结果无法核对，已暂停后续发送"
                self._abort_uncertain_log(message)
                raise GrokSetupError(message) from error
            committed = next((entry for entry in entries if entry.get("uuid") == marker
                              and entry.get("subtype") == "compact_boundary"
                              and entry.get("native_session") == session), None)
            if committed is None:
                raise
            return committed

    def _publish_compact(self, entry: dict) -> None:
        if self._log_sync_failed:
            return
        self._compact_recorded = True
        self._used = None
        self._events.on_entry(entry)
        self._events.on_usage(None, self._conv.context_window, None)

    def _abort_uncertain_log(self, error: str) -> None:
        """Latch the append prohibition before another buffered native frame can arrive."""
        if self._log_sync_failed:
            return
        active = self._prompt_id is not None or self._compact_task is not None
        self._log_sync_failed = self._closing = True
        self._log_abort_error = error
        self._prompt_id = self._pending_user = None
        self._text, self._message_id = "", None
        self._tools.clear()
        self._asks.clear()
        if self._compact_task is not None and self._compact_task is not asyncio.current_task():
            self._compact_task.cancel()
        for future in self._calls.values():
            if not future.done():
                future.set_exception(GrokSetupError(error))
        self._log_abort_task = asyncio.create_task(self._close_uncertain_log(error, active))

    async def _close_uncertain_log(self, error: str, active: bool) -> None:
        try:
            self._events.on_notice(error)
        except Exception:
            log.exception("grok: uncertain-log notice failed")
        try:
            task = self._compact_task
            if task is not None and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            await self._proc.close()
        except Exception:
            log.exception("grok: uncertain-log close failed")
        finally:
            if active:
                try:
                    self._events.on_turn_end(None, error)
                except Exception:
                    log.exception("grok: uncertain-log terminal callback failed")

    async def interrupt(self) -> None:
        if self._compact_task is not None:
            self._compact_task.cancel()
            try:
                await self._proc.send_json({"jsonrpc": "2.0", "method": "session/cancel",
                                           "params": {"sessionId": self._session}})
            except OSError:
                pass
            return
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
        self._closing = True
        if self._compact_task is not None and self._compact_task is not asyncio.current_task():
            task = self._compact_task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._log_abort_task is not None and self._log_abort_task is not asyncio.current_task():
            await asyncio.shield(self._log_abort_task)
        await self._proc.close()

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _call(self, method: str, params: dict, *, timeout_s: float | None = None) -> dict:
        rpc_id = self._new_id()
        future = asyncio.get_running_loop().create_future()
        self._calls[rpc_id] = future
        try:
            await self._proc.send_json({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout_s or self._setup_timeout_s)
        except TimeoutError as e:
            raise GrokSetupError(f"grok 没有响应（{method}）") from e
        finally:
            self._calls.pop(rpc_id, None)

    def _on_message(self, msg: dict) -> None:
        if self._log_sync_failed:
            return
        method, rpc_id = msg.get("method"), msg.get("id")
        if method is None:
            self._on_response(rpc_id, msg)
        elif rpc_id is not None:
            self._on_request(rpc_id, method, msg.get("params") or {})
        elif method in ("session/update", "_x.ai/session_notification", "_x.ai/session/update"):
            params = msg.get("params") or {}
            if params.get("sessionId") == self._session:
                if (params.get("_meta") or {}).get("isReplay"):
                    update = params.get("update") or {}
                    if update.get("sessionUpdate") == "user_message_chunk":
                        index = (update.get("_meta") or {}).get("promptIndex")
                        if isinstance(index, int):
                            self._replay_prompts[index] = (update.get("content") or {}).get("text") or ""
                    return
                self._on_update(params.get("update") or {})

    def _on_response(self, rpc_id: object, msg: dict) -> None:
        if self._log_sync_failed:
            return
        error = msg.get("error")
        if rpc_id is not None and rpc_id == self._prompt_id:
            if error:
                detail = error.get("data")
                self._end_turn(None, f"{error.get('message') or error}{': ' + str(detail) if detail else ''}")
            else:
                result = msg.get("result") or {}
                if self._pending_user is not None and result.get("stopReason") == "end_turn" and (
                        self._text or (result.get("_meta") or {}).get("promptId")):
                    user_uuid, _ = self._pending_user
                    if self._log.native_id(user_uuid) is None:
                        prompt = (result.get("_meta") or {}).get("promptId") or str(rpc_id)
                        self._log.bind_native(user_uuid, f"accepted:{prompt}")
                self._ingest_prompt_usage(result.get("_meta") or {})
                self._end_turn(result.get("stopReason"), None)
            return
        future = self._calls.get(rpc_id)
        if future is None or future.done():
            return
        if error:
            detail = error.get("data")
            future.set_exception(_RpcError(f"{error.get('message') or error}{': ' + str(detail) if detail else ''}"))
        else:
            future.set_result(msg.get("result") or {})

    def _on_request(self, rpc_id: int | str, method: str, params: dict) -> None:
        if self._log_sync_failed:
            return
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
        if self._log_sync_failed:
            return
        kind = u.get("sessionUpdate")
        if kind == "user_message_chunk" and self._pending_user is not None:
            user_uuid, text = self._pending_user
            index = (u.get("_meta") or {}).get("promptIndex")
            if isinstance(index, int) and (u.get("content") or {}).get("text") == text:
                self._log.bind_native(user_uuid, str(index))
        elif kind == "agent_message_chunk":
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
        elif kind == "session_info_update" and u.get("title"):
            self._title = u["title"]
            self._events.on_title(self._title)
        elif kind == "retry_state":
            self._events.on_retry(u.get("reason") if u.get("type") != "exhausted" else None)
            if u.get("is_rate_limited"):
                self._events.on_limits({"rate_limited": True, "reason": u.get("reason")})
        elif kind == "auto_compact_completed":
            self._record_compact("manual" if self._manual_compact else "auto", u.get("tokens_before"))

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
        if self._log_sync_failed:
            return
        call_id = str(u.get("toolCallId", ""))
        if kind == "tool_call":
            self._flush_text()
            self._tools[call_id] = {"name": u.get("title") or "tool", "input": {}}
        tracked = self._tools.setdefault(call_id, {"name": u.get("title") or "tool", "input": {}})
        if u.get("title"):
            tracked["name"] = u["title"]
        if u.get("rawInput"):
            tracked["input"] = self._abs_paths(u["rawInput"])
        if kind == "tool_call" or u.get("rawInput"):
            name, tool_input = grok_tool(tracked["name"], tracked["input"])
            self._events.on_entry(self._log.message({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]}}))
        status = u.get("status")
        if status in ("completed", "failed"):
            raw = u.get("rawOutput") or {}
            output = raw.get("error") or raw.get("output") or _content_text(u.get("content"))
            self._finish_tool(call_id, str(output or ""), failed=status == "failed")

    def _finish_tool(self, call_id: str, output: str, failed: bool) -> None:
        if self._log_sync_failed:
            return
        self._tools.pop(call_id)
        self._events.on_entry(self._log.message({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": output, "is_error": failed}]}}))
        self._closed_turn = False

    def _flush_text(self, usage: dict | None = None) -> None:
        if self._log_sync_failed:
            return
        if self._text:
            self._write_text(self._text, usage)
            self._text = ""

    def _write_text(self, text: str, usage: dict | None = None) -> None:
        if self._log_sync_failed:
            return
        message = {"role": "assistant", "content": [{"type": "text", "text": text}], **(usage or {})}
        self._events.on_entry(self._log.message({"type": "assistant", "message": message}))
        self._closed_turn = True

    def _end_turn(self, stop: str | None, error: str | None, notify: bool = True) -> None:
        if self._log_sync_failed:
            return
        self._events.on_retry(None)
        self._prompt_id = None
        self._pending_user = None
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
        if not self._closing and self._compact_task is not None:
            self._compact_task.cancel()
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
