"""One authenticated OpenCode 2 HTTP server per active chat.

V2 owns native messages, inbox admission, forks and compaction. AppLog remains the shared app transcript.
The event stream is volatile: losing it ends the app turn explicitly and never resends an uncertain input.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import uuid
from pathlib import Path
from urllib.parse import urlencode

from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.events import LinkEvents
from lynch_reader.chat.index import Conversation
from lynch_reader.chat.opencode import (OPENCODE_MODE, SETUP_TIMEOUT_S, OpencodeConfig, claude_tool,
                                        config_overlay, v2_mcp_servers, opencode_argv, write_rules)
from lynch_reader.chat.opencode_http import OpencodeHttp, OpencodeHttpError
from lynch_reader.chat.process import ChatProcess
from lynch_reader.chat.transcript import current_branch

INTERRUPTED_OUTPUT = "已中断"
TURN_ENDED_TEXT = "（opencode 结束了本轮）"


class OpencodeSetupError(Exception):
    pass


class OpencodeLink:
    def __init__(self, cfg: OpencodeConfig, conv: Conversation, env: dict[str, str], log: AppLog,
                 events: LinkEvents, *, setup_timeout_s: float = SETUP_TIMEOUT_S) -> None:
        self._cfg, self._conv, self._env, self._log, self._events = cfg, conv, env, log, events
        self._setup_timeout_s = setup_timeout_s
        self._session: str | None = None
        self.fork_native_ids: dict[str, str] = {}
        self._proc: ChatProcess | None = None
        self._native_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._operation = object()
        self._verifications: dict[asyncio.Task, object] = {}
        self._closing = False
        self._broken = False
        self._stream_error: str | None = None
        self._ready = asyncio.Event()
        self._prompt_id: str | None = None
        self._admitting = False
        self._stop_requested = False
        self._settling_stop = False
        self._interrupt_lock = asyncio.Lock()
        self._execution_started = False
        self._cancelled_inputs: set[str] = set()
        self._deferred_end: tuple | None = None
        self._compacting = False
        self._native_compacting = False
        self._compact_cancelled = False
        self._compact_done: asyncio.Future | None = None
        self._compact_execution_done: asyncio.Future | None = None
        self._compact_input: str | None = None
        self._compact_error: str | None = None
        self._artifact_owner = secrets.token_hex(8)
        self._hint_path = cfg.logs / f"{conv.id}.compact-hint.json"
        self._tools: dict[tuple[str, str], dict] = {}
        self._finished_tools: set[tuple[str, str]] = set()
        self._text = ""
        self._message_id: tuple | None = None
        self._closed_turn = True
        self._asks: dict[str, str] = {}
        self._used: int | None = None
        self._window: int | None = None
        self._cost: float | None = None
        self._title: str | None = None
        boundaries = {e["uuid"] for e in current_branch(log.entries()) if e.get("subtype") == "compact_boundary"}
        self._completed_compactions = {native for uuid in boundaries if (native := log.native_id(uuid))}
        self.session_lost = False

    @property
    def alive(self) -> bool:
        return not self._broken and self._proc is not None and self._proc.alive

    async def start(self) -> str:
        if self._broken or self._closing:
            raise OpencodeSetupError("此 opencode 连接已结束，请通过新连接恢复原会话")
        try:
            self._clear_hint(force=True)
            self._proc = self._make_process()
            await self._proc.start()
            async with asyncio.timeout(self._setup_timeout_s):
                while True:
                    if not self.alive:
                        raise OpencodeSetupError(self._exit_error or "opencode 进程退出")
                    try:
                        await self._http.request("GET", "/api/info", timeout=1)
                        break
                    except OpencodeHttpError as exc:
                        if exc.status != 503:
                            raise
                        await asyncio.sleep(0.05)
                    except (OSError, TimeoutError):
                        await asyncio.sleep(0.05)
                if self._conv.opencode_session:
                    self._session = self._conv.opencode_session
                    info = (await self._http.request("GET", self._path()))["data"]
                    await self._configure_session(info)
                else:
                    info = (await self._http.request("POST", "/api/session", {
                        "location": {"directory": str(self._cfg.repo_root)},
                        "agent": OPENCODE_MODE, "model": self._model(),
                    }))["data"]
                    self._session = info["id"]
                # Session creation only persists metadata; a location catalogue boots its plugin runtime.
                await self._http.request("GET", "/api/plugin")
                while not self._plugin_ready.exists():
                    if not self.alive:
                        raise OpencodeSetupError(self._exit_error or "opencode 插件加载失败")
                    await asyncio.sleep(0.05)
                inbox = await self._http.request("GET", self._path("/inbox"))
                if any(item["type"] == "compaction" for item in inbox["data"]):
                    raise OpencodeSetupError("原生会话仍有待核实的压缩请求，未恢复执行；请先核实原会话")
                messages = await self._messages()
                await self._reconcile_admission(messages)
                if inbox["data"]:
                    raise OpencodeSetupError("原生会话仍有待核实的消息，未恢复执行；请先核实原会话")
                await self._sync_compactions(messages)
                models = await self._http.request("GET", "/api/model")
                selected = next((m for m in models["data"] if m["providerID"] == self._model()["providerID"]
                                 and m["id"] == self._model()["id"]), None)
                self._window = selected.get("limit", {}).get("context") if selected else None
                self._native_task = asyncio.create_task(self._watch_native())
                await self._ready.wait()
            return self._session
        except BaseException as exc:
            await self.close()
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, TimeoutError):
                raise OpencodeSetupError("opencode 没有响应（启动私有服务）") from exc
            raise OpencodeSetupError(str(exc)) from exc

    def _model(self) -> dict:
        provider, model = self._conv.model.split("/", 1)
        return {"providerID": provider, "id": model}

    def _path(self, suffix: str = "") -> str:
        return f"/api/session/{self._session}{suffix}"

    def _make_process(self) -> ChatProcess:
        password = secrets.token_urlsafe(32)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self._http = OpencodeHttp(port, password, str(self._cfg.repo_root))
        self._exit_error = ""
        overlay = config_overlay(write_rules(self._cfg, self._conv))
        overlay["mcp"] = {"servers": v2_mcp_servers(self._cfg.repo_root)}
        plugin = self._cfg.logs / f"{self._conv.id}.{self._artifact_owner}.compaction-plugin"
        plugin.mkdir(parents=True, exist_ok=True)
        self._plugin_ready = plugin / "ready"
        self._plugin_ready.unlink(missing_ok=True)
        # A local plugin needs no npm dependencies. The one-shot file is consumed before the model call;
        # the app also removes it on completion, errors, cancellation and process recreation.
        (plugin / "index.js").write_text(
            'import { readFile, unlink, writeFile } from "node:fs/promises";\n'
            'export default { id: "lynch-compaction", async setup(ctx) {\n'
            '  await ctx.session.hook("compaction", async (event) => {\n'
            f'    const path = {json.dumps(str(self._hint_path))};\n'
            '    let hint;\n'
            '    try { hint = JSON.parse(await readFile(path, "utf8")); }\n'
            '    catch (error) { if (error.code === "ENOENT") return; throw error; }\n'
            f'    if (event.sessionID !== hint.sessionID || hint.owner !== {json.dumps(self._artifact_owner)}) return;\n'
            '    await unlink(path);\n'
            '    const instruction = "用户本次压缩的保留说明：请在摘要中逐字引用下面的说明，" +\n'
            '      "将它与此前已发生的历史事实区分，并保留其余必要上下文。\\n" + hint.text;\n'
            '    event.system.push({ type: "text", text: instruction });\n'
            '    event.messages.push({ role: "user", content: [{ type: "text", text: instruction }] });\n'
            '  });\n'
            f'  await writeFile({json.dumps(str(self._plugin_ready))}, "ready");\n'
            '} };\n', encoding="utf-8")
        overlay["plugins"] = [str(plugin)]
        env = {**self._env, "OPENCODE_PASSWORD": password,
               "OPENCODE_CONFIG_CONTENT": json.dumps(overlay, ensure_ascii=False)}
        return ChatProcess([*opencode_argv(self._cfg), "--hostname", "127.0.0.1", "--port", str(port)],
                           env, self._cfg.repo_root, on_message=lambda _msg: None,
                           on_exit=lambda _p, expected, err: self._on_exit(expected, err))

    async def _configure_session(self, info: dict) -> None:
        if info.get("agent") != OPENCODE_MODE:
            await self._http.request("POST", self._path("/agent"), {"agent": OPENCODE_MODE})
        if info.get("model") != self._model():
            await self._http.request("POST", self._path("/model"), {"model": self._model()})

    async def prompt(self, text: str) -> None:
        if (self._broken or self._closing or self._compacting or self._prompt_id
                or self._native_compacting or self._has_verifications()):
            raise OpencodeSetupError("opencode 当前不能接收新输入，请先恢复原对话或等待本轮结束")
        self._operation = object()
        native_id = "msg_" + uuid.uuid4().hex
        entry = self._log.user(text, opencode_input_id=native_id)
        self._events.on_entry(entry)
        self._prompt_id = native_id
        self._used = None
        self._closed_turn = False
        self._finished_tools.clear()
        self._admitting = True
        self._stop_requested = False
        self._execution_started = False
        try:
            result = (await self._http.request("POST", self._path("/prompt"),
                                               {"id": native_id, "text": text}))["data"]
            if result.get("id") != native_id or result.get("sessionID") != self._session:
                raise OpencodeSetupError("opencode 接收的消息编号不一致；请保留本轮记录核实")
            self._log.bind_native(entry["uuid"], native_id)
        except BaseException:
            if not self._closing:
                await self._abort_uncertain("无法核实 opencode 是否已接收本轮消息")
            raise
        finally:
            self._admitting = False
        if self._stop_requested:
            await self._interrupt_admitted()
        self._flush_deferred_end()

    def _flush_deferred_end(self) -> None:
        if self._deferred_end:
            owner, stop, error, notify = self._deferred_end
            self._deferred_end = None
            if owner is self._operation and not self._broken and not self._closing:
                self._end_turn(stop, error, notify)

    def _has_verifications(self) -> bool:
        return any(owner is self._operation for owner in self._verifications.values())

    def _owns_operation(self, session: str, owner: object) -> bool:
        return (session == self._session and owner is self._operation
                and not self._broken and not self._closing)

    def _clear_hint(self, *, force: bool = False) -> None:
        if not force:
            try:
                hint = json.loads(self._hint_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                return
            if not isinstance(hint, dict) or hint.get("owner") != self._artifact_owner:
                return
        self._hint_path.unlink(missing_ok=True)

    def _retire(self) -> None:
        """Invalidate ownership before any terminal callback or buffered HTTP response can run."""
        if self._broken:
            return
        self._broken = True
        self._operation = object()
        self._deferred_end = None
        self._clear_hint()
        self._end_turn(None, None, notify=False)

    async def interrupt(self) -> None:
        self._stop_requested = True
        self._compact_cancelled = self._compacting
        # The admission owner cancels again immediately after the durable ACK. Deleting before it
        # exists cannot stop an HTTP request that is still being admitted on the native server.
        if self._admitting:
            return
        if self._compacting and self._compact_input is None:
            return
        await self._interrupt_admitted()

    async def _interrupt_admitted(self) -> None:
        pending = self._prompt_id or self._compact_input
        async with self._interrupt_lock:
            if self._broken or pending != (self._prompt_id or self._compact_input):
                return
            await self._cancel_native_input(pending)

    async def _cancel_native_input(self, pending: str | None) -> None:
        self._settling_stop = True
        try:
            if pending:
                await self._http.request("DELETE", self._path(f"/inbox/{pending}"))
            result = await self._http.request("POST", self._path("/interrupt?resume=false"))
            if result.get("interrupted") is False and pending in self._cancelled_inputs:
                self._settle_cancelled_input(pending)
        except BaseException:
            if not self._closing:
                await self._abort_uncertain("无法核实 opencode 是否已停止本轮执行")
            raise
        finally:
            self._settling_stop = False
            self._clear_hint()
        self._flush_deferred_end()

    def _settle_cancelled_input(self, native_id: str) -> None:
        if self._execution_started or not self._stop_requested:
            return
        if native_id == self._compact_input:
            if not self._compact_done.done():
                self._compact_done.set_result(None)
            if not self._compact_execution_done.done():
                self._compact_execution_done.set_result(("cancelled", None))
        elif native_id == self._prompt_id:
            self._end_turn("cancelled", None)

    async def _abort_uncertain(self, reason: str) -> None:
        if self._broken:
            return
        self._retire()
        self._write_text(f"opencode 本轮出错：{reason}；原生状态待核实，请恢复原对话后再继续")
        if self._proc:
            await self._proc.close()

    async def answer(self, request_id: str, allow: bool) -> None:
        session = self._asks.pop(request_id, None)
        if session is not None:
            await self._reply_permission(session, request_id, allow)

    async def _reply_permission(self, session: str, request_id: str, allow: bool) -> None:
        await self._http.request("POST", f"/api/session/{session}/permission/{request_id}/reply",
                                 {"decision": "once" if allow else "reject"})

    async def close(self) -> None:
        self._closing = True
        self._retire()
        if self._native_task:
            self._native_task.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, *([self._native_task] if self._native_task else []),
                             return_exceptions=True)
        self._native_task = None
        if self._proc:
            await self._proc.close()

    async def _messages(self, session: str | None = None) -> list[dict]:
        path = f"/api/session/{session or self._session}/message"
        query = "order=asc"
        messages = []
        seen = set()
        while True:
            page = await self._http.request("GET", path + "?" + query)
            messages.extend(page["data"])
            cursor = page.get("cursor", {}).get("next")
            if not cursor:
                return messages
            if cursor in seen:
                raise OpencodeSetupError("opencode 消息分页重复，无法核实完整历史")
            seen.add(cursor)
            query = urlencode({"cursor": cursor})

    async def rewind(self, message_uuid: str) -> str:
        if (self._broken or self._closing or self._prompt_id is not None or self._compacting
                or self._native_compacting or self._has_verifications()):
            raise OpencodeSetupError("请等待当前回答结束后再编辑")
        branch = current_branch(self._log.entries())
        entry = next((e for e in branch if e.get("uuid") == message_uuid), None)
        if entry is None or entry.get("type") != "user" or not isinstance(entry["message"]["content"], str):
            raise OpencodeSetupError("找不到要编辑的用户消息")
        messages = await self._messages()
        native_id = self._log.native_id(message_uuid)
        boundary = next((i for i, message in enumerate(messages)
                         if message["id"] == native_id and message["type"] == "user"), None)
        if boundary is None:
            raise OpencodeSetupError("旧对话消息缺少可核实的原生编号，无法唯一匹配，未切换对话分支")
        try:
            new = (await self._http.request("POST", self._path("/fork"), {"before": native_id}))["data"]["id"]
            copied = await self._messages(new)
        except Exception as exc:
            raise OpencodeSetupError(str(exc)) from exc
        if len(copied) != boundary or any(a["type"] != b["type"] for a, b in zip(messages[:boundary], copied)):
            raise OpencodeSetupError("opencode 分叉边界与原始消息不一致")
        if self._native_compacting or self._has_verifications():
            raise OpencodeSetupError("原生压缩结果尚待核实，未切换对话分支")
        mapping = {a["id"]: b["id"] for a, b in zip(messages[:boundary], copied)}
        self._session = new
        self._operation = object()
        self._used = None
        self._title = None
        self._completed_compactions = {mapping.get(mid, mid) for mid in self._completed_compactions}
        self.fork_native_ids = {retained["uuid"]: mapped for retained in branch
                                if (mapped := mapping.get(self._log.native_id(retained["uuid"])))}
        return new

    async def compact(self, hint: str = "") -> None:
        if (self._broken or self._closing or self._prompt_id is not None or self._compacting
                or self._native_compacting or self._has_verifications()):
            raise OpencodeSetupError("请等待当前回答结束后再压缩")
        self._operation = object()
        self._compacting = True
        self._compact_cancelled = False
        self._stop_requested = False
        self._execution_started = False
        self._compact_error = None
        self._compact_done = asyncio.get_running_loop().create_future()
        self._compact_execution_done = asyncio.get_running_loop().create_future()
        self._events.on_state("compacting")
        self._spawn(self._run_compaction(hint))

    async def _run_compaction(self, hint: str) -> None:
        error = None
        try:
            if self._compact_cancelled:
                return
            input_id = "msg_" + uuid.uuid4().hex
            self._compact_input = input_id
            if hint:
                self._hint_path.write_text(json.dumps({"sessionID": self._session, "inputID": input_id,
                                                       "owner": self._artifact_owner, "text": hint},
                                                      ensure_ascii=False), encoding="utf-8")
            self._admitting = True
            try:
                result = (await self._http.request("POST", self._path("/compact"), {"id": input_id}))["data"]
            finally:
                self._admitting = False
            if result.get("id") != input_id:
                raise OpencodeSetupError("opencode 压缩请求编号不一致")
            if self._stop_requested:
                await self._interrupt_admitted()
            # A verified summary is not the execution boundary. The terminal native event must
            # also arrive before the app can drain its next queued input.
            settled = await asyncio.wait_for(asyncio.gather(self._compact_done, self._compact_execution_done,
                                                           return_exceptions=True), 600)
            for outcome in settled:
                if isinstance(outcome, BaseException):
                    raise outcome
            stop, terminal_error = settled[1]
            error = self._compact_error or terminal_error
            if stop == "cancelled" and not self._stop_requested:
                error = error or "opencode 原生压缩执行意外中断，原对话仍保留"
        except asyncio.CancelledError:
            self._compact_cancelled = True
            raise
        except Exception as exc:
            error = str(exc)
            await self._abort_uncertain(f"无法核实本轮压缩是否已接收或完成：{error}")
        finally:
            self._clear_hint()
            for future in (self._compact_done, self._compact_execution_done):
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()
            if not self._broken and not self._closing:
                self._end_turn("cancelled" if self._compact_cancelled else "end_turn", error)

    async def generate_title(self, description: str) -> str | None:
        info = (await self._http.request("GET", self._path()))["data"]
        title = info.get("title") or self._title
        return title if title and not title.startswith("New session") else None

    async def _reconcile_admission(self, messages: list[dict]) -> None:
        received = {m["id"] for m in messages if m["type"] == "user"}
        branch = current_branch(self._log.entries())
        uncertain = [e for e in branch if e.get("opencode_input_id")
                     and self._log.native_id(e["uuid"]) is None]
        for entry in uncertain:
            native_id = entry.get("opencode_input_id")
            if native_id in received and self._log.native_id(entry["uuid"]) is None:
                self._log.bind_native(entry["uuid"], native_id)

    async def _sync_compactions(self, messages: list[dict] | None = None, *,
                                session: str | None = None, owner: object | None = None) -> bool:
        session = session or self._session
        owner = self._operation if owner is None else owner
        messages = messages if messages is not None else await self._messages(session)
        if not self._owns_operation(session, owner):
            return False
        for message in messages:
            if message["type"] == "compaction" and message.get("status") == "completed":
                self._finish_compaction(message["id"], message["reason"])
        return True

    def _finish_compaction(self, native_id: str, trigger: str) -> None:
        if native_id in self._completed_compactions:
            return
        self._completed_compactions.add(native_id)
        boundary = self._log.compacted(trigger, self._used)
        self._log.bind_native(boundary["uuid"], native_id)
        self._events.on_entry(boundary)
        self._used = None

    async def _watch_native(self) -> None:
        try:
            async for event in self._http.events():
                if event.get("type") == "server.connected":
                    self._ready.set()
                self._on_native(event)
            raise OpencodeSetupError("opencode 事件连接中断；本轮状态需要核实")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing and self.alive:
                self._stream_error = str(exc)
                self._retire()
                if self._compact_done and not self._compact_done.done():
                    self._compact_done.set_exception(exc)
                # A link without its event channel cannot accept another turn safely.
                self._spawn(self._proc.close())

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_native(self, event: dict) -> None:
        if self._broken:
            return
        data = event.get("data") or {}
        kind = event.get("type")
        if kind == "permission.asked":
            self._permission(data)
            return
        if data.get("sessionID") != self._session:
            return
        if kind == "session.renamed":
            title = data.get("title")
            if title and title != self._title and not title.startswith("New session"):
                self._title = title
                self._events.on_title(title)
        elif kind == "session.retry.scheduled":
            self._events.on_retry(_error_text(data.get("error")) or "opencode 正在重试")
        elif kind in ("session.execution.started", "session.step.started"):
            if kind == "session.execution.started" and (self._prompt_id or self._compacting):
                self._execution_started = True
            self._events.on_retry(None)
        elif kind == "session.inbox.cancelled":
            self._cancelled_inputs.add(data["inboxID"])
            self._settle_cancelled_input(data["inboxID"])
        elif kind == "session.compaction.started":
            self._native_compacting = True
            self._events.on_state("compacting")
        elif kind == "session.compaction.ended":
            # Capture ownership before scheduling: the coroutine may not start until a later turn.
            owner = self._operation
            manual_input = self._compact_input if self._compacting else None
            manual_done = self._compact_done if manual_input is not None else None
            task = asyncio.create_task(self._compaction_ended(data["sessionID"], owner, manual_input, manual_done))
            self._verifications[task] = owner
            self._tasks.add(task)
            task.add_done_callback(self._verification_finished)
        elif kind == "session.compaction.failed":
            self._native_compacting = False
            if self._compacting and self._compact_done and not self._compact_done.done():
                self._compact_error = _error_text(data["error"])
                self._compact_done.set_result(None)
            elif not self._compacting:
                self._events.on_state("running" if self._prompt_id else "idle")
            self._flush_deferred_end()
        elif kind in ("session.text.started", "session.text.delta", "session.text.ended") and self._prompt_id:
            key = (data["assistantMessageID"], data["ordinal"])
            if key != self._message_id:
                self._flush_text()
                self._message_id = key
            if kind == "session.text.delta":
                self._text += data["delta"]
                self._events.on_delta(data["delta"])
            elif kind == "session.text.ended":
                self._text = data["text"]
        elif kind.startswith("session.tool.") and self._prompt_id:
            self._tool_event(kind, data)
        elif kind == "session.step.ended" and self._prompt_id:
            tokens = data.get("tokens")
            if tokens:
                cache = tokens.get("cache", {})
                self._used = tokens["input"] + tokens["output"] + cache.get("read", 0) + cache.get("write", 0)
                self._events.on_usage(self._used, self._window, self._cost)
        elif kind == "session.usage.updated":
            self._cost = data.get("cost")
            if self._prompt_id:
                self._events.on_usage(self._used, self._window, self._cost)
        elif kind in ("session.execution.succeeded", "session.execution.failed", "session.execution.interrupted"):
            stop = "cancelled" if kind.endswith("interrupted") else "end_turn"
            error = _error_text(data["error"]) if kind.endswith("failed") else None
            if stop == "cancelled" or error:
                self._native_compacting = False
            if self._compacting:
                if self._compact_execution_done and not self._compact_execution_done.done():
                    self._compact_execution_done.set_result((stop, error))
                if (error or stop == "cancelled") and self._compact_done and not self._compact_done.done():
                    self._compact_done.set_result(None)
                return
            if self._prompt_id:
                stop = "cancelled" if self._stop_requested else stop
                self._end_turn(None if error else stop, error)

    def _verification_finished(self, task: asyncio.Task) -> None:
        owner = self._verifications.pop(task)
        self._tasks.discard(task)
        if owner is self._operation:
            self._flush_deferred_end()

    async def _compaction_ended(self, session: str, owner: object, manual_input: str | None,
                                manual_done: asyncio.Future | None) -> None:
        try:
            if not self._owns_operation(session, owner):
                return
            if not (manual_input is not None and self._compact_cancelled):
                if not await self._sync_compactions(session=session, owner=owner):
                    return
                if manual_input is not None and manual_input not in self._completed_compactions:
                    raise OpencodeSetupError("无法核实本轮原生摘要已完成")
            self._native_compacting = False
            if manual_done is not None and not manual_done.done():
                manual_done.set_result(None)
            elif manual_input is None:
                self._events.on_state("running" if self._prompt_id else "idle")
        except Exception as exc:
            if not self._owns_operation(session, owner):
                return
            if manual_done is not None and not manual_done.done():
                manual_done.set_exception(exc)
            else:
                await self._abort_uncertain("无法核实原生自动压缩结果")

    def _permission(self, data: dict) -> None:
        session, request_id = data.get("sessionID"), data.get("id")
        if not session or not request_id:
            return
        source = data.get("source") or {}
        tracked = self._tools.get((source.get("messageID"), source.get("id")))
        valid = (session == self._session and self._prompt_id and source.get("type") == "tool" and tracked
                 and source.get("messageID") == tracked["message_id"] and data.get("action") == "edit")
        name, tool_input = claude_tool(tracked["name"], tracked["input"]) if valid else ("", {})
        path = tool_input.get("file_path")
        skill = (self._cfg.repo_root / "frameworks/.claude/skills/sediment-framework").resolve()
        if valid and name in ("Edit", "Write") and path:
            target = Path(path)
            target = target if target.is_absolute() else self._cfg.repo_root / target
            target = target.resolve()
            resources = data.get("resources") or []
            resolved = [(Path(resource) if Path(resource).is_absolute() else self._cfg.repo_root / resource).resolve()
                        for resource in resources]
            valid = target.is_relative_to(skill) and bool(resolved) and all(r == target for r in resolved)
        else:
            valid = False
        if not valid:
            self._spawn(self._reply_permission(session, request_id, False))
            return
        # V2 paths are relative to the fixed session location; shared Claude approvals require absolute paths.
        tool_input = {**tool_input, "file_path": str(target)}
        self._asks[request_id] = session
        self._events.on_permission(request_id, tracked["public_id"], name, tool_input)

    def _tool_event(self, kind: str, data: dict) -> None:
        call_id = data.get("id")
        key = (data["assistantMessageID"], call_id)
        if not call_id or key in self._finished_tools:
            return
        if kind == "session.tool.input.started":
            self._flush_text()
            public_id = json.dumps([self._session, data["assistantMessageID"], call_id], separators=(",", ":"))
            self._tools[key] = {"name": data["name"], "input": {}, "json": "", "public_id": public_id,
                                "message_id": data["assistantMessageID"]}
            name, tool_input = claude_tool(data["name"], {})
            self._events.on_entry(self._log.message({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "tool_use", "id": public_id, "name": name, "input": tool_input}]}}))
            return
        tracked = self._tools.get(key)
        if tracked is None:
            return
        if kind == "session.tool.input.delta":
            tracked["json"] += data["delta"]
        elif kind == "session.tool.input.ended":
            tracked["json"] = data["text"]
        elif kind == "session.tool.called":
            tracked["input"].update(data["input"])
            name, tool_input = claude_tool(tracked["name"], tracked["input"])
            self._events.on_entry(self._log.message({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "tool_use", "id": tracked["public_id"], "name": name, "input": tool_input}]}}))
        elif kind in ("session.tool.success", "session.tool.failed"):
            output = "\n".join(c["text"] for c in data.get("content", []) if c.get("type") == "text")
            self._finish_tool(key, output or _error_text(data.get("error")), kind.endswith("failed"))

    def _finish_tool(self, key: tuple[str, str], output: str, failed: bool) -> None:
        tracked = self._tools.pop(key)
        self._finished_tools.add(key)
        self._events.on_entry(self._log.message({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tracked["public_id"], "content": output, "is_error": failed}]}}))
        self._closed_turn = False

    def _flush_text(self, usage: dict | None = None) -> None:
        if self._text:
            self._write_text(self._text, usage)
            self._text = ""

    def _write_text(self, text: str, usage: dict | None = None) -> None:
        self._events.on_entry(self._log.message({"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}], **(usage or {})}}))
        self._closed_turn = True

    def _end_turn(self, stop: str | None, error: str | None, notify: bool = True) -> None:
        if not self._broken and (self._admitting or self._settling_stop
                                 or self._native_compacting or self._has_verifications()):
            self._deferred_end = (self._operation, stop, error, notify)
            return
        # Every turn uses the same settlement gate. Native completion can precede the HTTP
        # cancellation response; retaining the compact input keeps the next turn blocked until then.
        if self._compacting:
            self._compact_input = None
            self._compacting = False
            self._native_compacting = False
            if notify:
                self._events.on_state("idle")
                self._events.on_turn_end(stop, error)
            return
        self._prompt_id = None
        usage = {"usage": {"input_tokens": self._used}} if self._used else None
        self._flush_text(None if error or not notify else usage)
        self._message_id = None
        for call_id in list(self._tools):
            self._finish_tool(call_id, INTERRUPTED_OUTPUT, True)
        self._asks.clear()
        if stop == "cancelled":
            self._events.on_entry(self._log.interrupted())
        elif notify and (error is not None or not self._closed_turn):
            self._write_text(f"opencode 本轮出错：{error}" if error else TURN_ENDED_TEXT, usage)
        if notify:
            self._events.on_turn_end(stop, error)

    def _on_exit(self, expected: bool, stderr_tail: str) -> None:
        self._retire()
        if self._stream_error and not self._closing:
            expected = False
            stderr_tail = stderr_tail or self._stream_error
        self._exit_error = stderr_tail
        for future in (self._compact_done, self._compact_execution_done):
            if future and not future.done():
                future.set_exception(OpencodeSetupError(stderr_tail or "opencode 进程退出"))
        self._events.on_exit(expected, stderr_tail)


def _error_text(error: dict | None) -> str:
    return str((error or {}).get("message") or "")
