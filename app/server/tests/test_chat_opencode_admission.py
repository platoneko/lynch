import asyncio

import pytest

from lynch_reader.chat.opencode_http import OpencodeHttpError
from lynch_reader.chat.opencode_link import OpencodeSetupError
from tests.chat_helpers import until
from tests.test_chat_opencode_link import make_link, oc_conv, run_turn
from tests.test_chat_opencode_parity import native


class NativeProcess:
    alive = True

    async def close(self):
        self.alive = False


class AdmissionServer:
    def __init__(self, link):
        self.link = link
        self.entered = asyncio.Event()
        self.admit = asyncio.Event()
        self.summary = asyncio.Event()
        self.admit.set()
        self.summary.set()
        self.input_id = None
        self.calls = []
        self.drop_ack = False

    async def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path))
        if path.endswith(("/compact", "/prompt")):
            self.entered.set()
            await self.admit.wait()
            self.input_id = body["id"]
            self.calls.append(("admitted", path))
            if self.drop_ack:
                raise OpencodeHttpError("synthetic accepted input lost its ACK")
            return {"data": {"id": self.input_id, "sessionID": self.link._session}}
        if "/message?" in path:
            await self.summary.wait()
            return {"data": [{"id": self.input_id, "type": "compaction", "status": "completed",
                               "reason": "manual"}], "cursor": {}}
        if method == "DELETE":
            self.link._on_native(native("session.inbox.cancelled", inboxID=self.input_id))
        if "/interrupt" in path:
            return {"interrupted": False}


def offline_link(tmp_path):
    link, rec = make_link(tmp_path)
    link._cfg.logs.mkdir(parents=True, exist_ok=True)
    link._session = "ses_test"
    link._proc = NativeProcess()
    link._http = AdmissionServer(link)
    return link, rec


@pytest.mark.parametrize("terminal_first", [False, True])
def test_manual_compaction_waits_for_summary_and_execution_terminal(tmp_path, terminal_first):
    async def main():
        link, rec = offline_link(tmp_path)
        server = link._http
        server.summary.clear()
        await link.compact()
        await server.entered.wait()
        link._on_native(native("session.compaction.ended", reason="manual", text="summary"))
        if terminal_first:
            link._on_native(native("session.execution.succeeded"))
            await asyncio.sleep(.02)
            assert rec.turn_ends == []
            server.summary.set()
        else:
            server.summary.set()
            await until(lambda: link._completed_compactions)
            await asyncio.sleep(.02)
            assert rec.turn_ends == []
            link._on_native(native("session.execution.succeeded"))
        await until(lambda: rec.turn_ends)
        assert rec.turn_ends == [("end_turn", None)]
        await link.prompt("next queued input")
        assert link._prompt_id is not None
        link._on_native(native("session.text.delta", assistantMessageID="msg_reply", ordinal=0, delta="answer"))
        assert rec.deltas == ["answer"]
        link._on_native(native("session.execution.succeeded"))
        assert len(rec.turn_ends) == 2
        await link.close()
    asyncio.run(main())


def test_failed_compaction_waits_for_native_execution_to_settle(tmp_path):
    async def main():
        link, rec = offline_link(tmp_path)
        await link.compact()
        await link._http.entered.wait()
        error = {"type": "APIError", "message": "summary failed"}
        link._on_native(native("session.compaction.failed", reason="manual", error=error))
        await asyncio.sleep(.02)
        assert rec.turn_ends == []
        link._on_native(native("session.execution.failed", error=error))
        await until(lambda: rec.turn_ends)
        assert rec.turn_ends == [("end_turn", "summary failed")]
        await link.close()
    asyncio.run(main())


@pytest.mark.parametrize("kind", ["compact", "prompt"])
def test_stop_during_admission_cancels_only_after_the_input_is_durable(tmp_path, kind):
    async def main():
        link, rec = offline_link(tmp_path)
        server = link._http
        server.admit.clear()
        operation = None
        if kind == "compact":
            await link.compact("preserve once")
        else:
            operation = asyncio.create_task(link.prompt("pending question"))
        await server.entered.wait()
        await asyncio.wait_for(link.interrupt(), .2)
        assert rec.turn_ends == []
        server.admit.set()
        if operation:
            await operation
        await until(lambda: rec.turn_ends)
        assert rec.turn_ends == [("cancelled", None)]
        admitted = next(i for i, call in enumerate(server.calls) if call[0] == "admitted")
        deletion = next(i for i, call in enumerate(server.calls) if call[0] == "DELETE")
        assert deletion > admitted
        assert not link._hint_path.exists()
        await link.close()
    asyncio.run(main())


@pytest.mark.parametrize("stop_requested", [False, True])
def test_unknown_compaction_admission_closes_the_link_before_another_input(tmp_path, stop_requested):
    async def main():
        link, rec = offline_link(tmp_path)
        server = link._http
        server.drop_ack = True
        server.admit.clear()
        await link.compact("one shot only")
        await server.entered.wait()
        if stop_requested:
            await link.interrupt()
        server.admit.set()
        await until(lambda: not link._tasks)
        assert not link.alive
        assert not link._hint_path.exists()
        with pytest.raises(OpencodeSetupError):
            await link.prompt("must not follow uncertain native work")
        assert not any(path.endswith("/prompt") for _, path in server.calls)
        await link.close()
    asyncio.run(main())


def test_recovery_rejects_an_unsettled_native_compaction_inbox(tmp_path):
    import json
    sid, _ = run_turn(tmp_path, "seed context")
    path = tmp_path / "oc-sessions" / f"{sid}.json"
    data = json.loads(path.read_text())
    data["inbox"] = [{"id": "msg_uncertain_compact", "type": "compaction", "sessionID": sid,
                      "time": {"created": 1}, "payload": {}, "delivery": "queue"}]
    path.write_text(json.dumps(data))

    async def main():
        link, _ = make_link(tmp_path, c=oc_conv(opencode_session=sid))
        with pytest.raises(OpencodeSetupError, match="压缩"):
            await link.start()
        assert not link.alive
    asyncio.run(main())


def test_recovery_preserves_pending_native_message_and_rejects_new_input(tmp_path):
    import json

    sid, _ = run_turn(tmp_path, "seed context")
    path = tmp_path / "oc-sessions" / f"{sid}.json"
    data = json.loads(path.read_text())
    pending = {"id": "msg_pending", "type": "user", "sessionID": sid,
               "time": {"created": 1}, "payload": {"text": "older message"}, "delivery": "queue"}
    data["inbox"] = [pending]
    path.write_text(json.dumps(data))

    async def main():
        link, _ = make_link(tmp_path, c=oc_conv(opencode_session=sid))
        with pytest.raises(OpencodeSetupError, match="消息"):
            await link.start()
        assert not link.alive
        with pytest.raises(OpencodeSetupError):
            await link.prompt("new message")
        assert json.loads(path.read_text())["inbox"] == [pending]
    asyncio.run(main())


def test_compaction_execution_timeout_closes_the_link(tmp_path, monkeypatch):
    real_wait = asyncio.wait_for

    async def shortened_wait(awaitable, timeout):
        return await real_wait(awaitable, .03 if timeout == 600 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", shortened_wait)

    async def main():
        link, rec = offline_link(tmp_path)
        await link.compact("keep once")
        await link._http.entered.wait()
        link._on_native(native("session.compaction.ended", reason="manual", text="summary"))
        await until(lambda: not link._tasks)
        assert not link.alive
        with pytest.raises(OpencodeSetupError):
            await link.prompt("must not enter an execution with no terminal")
        assert not link._hint_path.exists()
        await link.close()
    asyncio.run(main())


@pytest.mark.parametrize("kind", ["compact", "prompt"])
@pytest.mark.parametrize("delayed", ["DELETE", "interrupt"])
@pytest.mark.parametrize("cancel_fails", [False, True])
def test_turn_release_waits_for_the_entire_cancellation_rpc(tmp_path, kind, delayed, cancel_fails):
    async def main():
        link, rec = offline_link(tmp_path)
        waiting = asyncio.Event()
        release = asyncio.Event()

        class DelayedCancellation(AdmissionServer):
            async def request(self, method, path, body=None, **kwargs):
                is_cancellation = method == "DELETE" or "/interrupt" in path
                phase = "DELETE" if method == "DELETE" else "interrupt"
                if is_cancellation and phase == delayed:
                    if method == "DELETE":
                        self.link._on_native(native("session.inbox.cancelled", inboxID=self.input_id))
                    waiting.set()
                    await release.wait()
                    if cancel_fails:
                        raise OpencodeHttpError("synthetic cancellation ACK lost")
                return await super().request(method, path, body, **kwargs)

        server = DelayedCancellation(link)
        link._http = server
        if kind == "compact":
            await link.compact()
            await server.entered.wait()
        else:
            await link.prompt("pending question")
        stopping = asyncio.create_task(link.interrupt())
        await waiting.wait()
        try:
            await asyncio.sleep(.02)
            assert rec.turn_ends == []
            if kind == "compact":
                assert link._compacting and link._compact_input == server.input_id
            with pytest.raises(OpencodeSetupError):
                await link.prompt("must wait for old cancellation")
        finally:
            release.set()
            if cancel_fails:
                with pytest.raises(OpencodeHttpError):
                    await stopping
            else:
                await stopping
        if cancel_fails:
            assert not link.alive
            assert rec.turn_ends == []
        else:
            await until(lambda: rec.turn_ends)
            assert rec.turn_ends == [("cancelled", None)]
            await link.prompt("next input after cancellation settled")
            assert link._prompt_id is not None
            link._on_native(native("session.text.delta", assistantMessageID="msg_reply", ordinal=0, delta="answer"))
            assert rec.deltas == ["answer"]
        await link.close()
    asyncio.run(main())
