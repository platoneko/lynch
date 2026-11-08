import asyncio

import pytest

from lynch_reader.chat.opencode_http import OpencodeHttpError
from lynch_reader.chat.opencode_link import OpencodeSetupError
from tests.chat_helpers import until
from tests.test_chat_opencode_admission import AdmissionServer, offline_link
from tests.test_chat_opencode_parity import native


@pytest.mark.parametrize("verification_fails", [False, True])
def test_auto_verification_settles_before_the_next_manual_compaction(tmp_path, verification_fails):
    async def main():
        link, rec = offline_link(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        class OldSummary(AdmissionServer):
            first = True

            async def request(self, method, path, body=None, **kwargs):
                if "/message?" in path and self.first:
                    self.first = False
                    entered.set()
                    await release.wait()
                    if verification_fails:
                        raise OpencodeHttpError("old automatic summary verification failed")
                    return {"data": [{"id": "msg_old_auto", "type": "compaction", "status": "completed",
                                       "reason": "auto"}], "cursor": {}}
                return await super().request(method, path, body, **kwargs)

        link._http = OldSummary(link)
        await link.prompt("old question")
        link._on_native(native("session.compaction.ended", reason="auto", text="old summary"))
        await entered.wait()
        link._on_native(native("session.execution.succeeded"))
        try:
            assert rec.turn_ends == []
            with pytest.raises(OpencodeSetupError):
                await link.compact("new manual hint must wait")
            with pytest.raises(OpencodeSetupError):
                await link.prompt("new user must wait for the old boundary")
        finally:
            release.set()
        await until(lambda: not link._tasks)
        if verification_fails:
            assert not link.alive
            with pytest.raises(OpencodeSetupError):
                await link.compact("must recover before retrying")
        else:
            assert rec.turn_ends == [("end_turn", None)]
            await link.compact("manual hint")
            await until(lambda: link._compact_input is not None)
            link._on_native(native("session.compaction.ended", reason="manual", text="new summary"))
            link._on_native(native("session.execution.succeeded"))
            await until(lambda: len(rec.turn_ends) == 2)
            assert not link._broken
            await link.prompt("new user after verification")
            entries = link._log.entries()
            old_boundary = next(i for i, entry in enumerate(entries)
                                if entry.get("subtype") == "compact_boundary")
            new_user = next(i for i, entry in enumerate(entries)
                            if entry.get("message", {}).get("content") == "new user after verification")
            assert old_boundary < new_user
        await link.close()
    asyncio.run(main())


def test_rewind_cannot_switch_session_while_native_verification_is_pending(tmp_path):
    async def main():
        link, rec = offline_link(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        class OldSummary(AdmissionServer):
            async def request(self, method, path, body=None, **kwargs):
                if "/message?" in path:
                    entered.set()
                    await release.wait()
                    return {"data": [{"id": "msg_old_auto", "type": "compaction", "status": "completed",
                                       "reason": "auto"}], "cursor": {}}
                return await super().request(method, path, body, **kwargs)

        server = OldSummary(link)
        link._http = server
        await link.prompt("old question")
        user = rec.entries[0]
        link._on_native(native("session.compaction.ended", reason="auto", text="old summary"))
        await entered.wait()
        link._on_native(native("session.execution.succeeded"))
        try:
            with pytest.raises(OpencodeSetupError):
                await asyncio.wait_for(link.rewind(user["uuid"]), .1)
            assert link._session == "ses_test"
            assert not any(path.endswith("/fork") for _, path in server.calls)
        finally:
            release.set()
        await until(lambda: not link._tasks)
        await link.close()
    asyncio.run(main())


def test_a_verification_response_after_close_cannot_append_an_old_boundary(tmp_path):
    async def main():
        link, rec = offline_link(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowSummary(AdmissionServer):
            async def request(self, method, path, body=None, **kwargs):
                if "/message?" in path:
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                    return {"data": [{"id": "msg_old_auto", "type": "compaction", "status": "completed",
                                       "reason": "auto"}], "cursor": {}}
                return await super().request(method, path, body, **kwargs)

        link._http = SlowSummary(link)
        await link.prompt("old question")
        link._on_native(native("session.compaction.ended", reason="auto", text="old summary"))
        await entered.wait()
        closing = asyncio.create_task(link.close())
        await asyncio.sleep(.01)
        release.set()
        await closing
        assert not any(entry.get("subtype") == "compact_boundary" for entry in rec.entries)
        assert rec.turn_ends == []
    asyncio.run(main())


@pytest.mark.parametrize("termination", ["expected_exit", "unexpected_exit", "stream_eof"])
@pytest.mark.parametrize("verification_fails", [False, True])
def test_every_native_termination_retires_buffered_verification(tmp_path, termination, verification_fails):
    import json

    async def main():
        link, rec = offline_link(tmp_path)
        entered, release, eof = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class BufferedSummary(AdmissionServer):
            async def request(self, method, path, body=None, **kwargs):
                if "/message?" in path:
                    entered.set()
                    await release.wait()
                    if verification_fails:
                        raise OpencodeHttpError("old synthetic verification failed")
                    return {"data": [{"id": "msg_old_auto_exit", "type": "compaction", "status": "completed",
                                       "reason": "auto"}], "cursor": {}}
                return await super().request(method, path, body, **kwargs)

            async def events(self):
                await eof.wait()
                if False:
                    yield {}

        original_close = link._proc.close

        async def reporting_close():
            if link._proc.alive:
                await original_close()
                link._on_exit(True, "synthetic EOF process close")

        link._proc.close = reporting_close
        link._http = BufferedSummary(link)
        await link.prompt("old synthetic question")
        link._on_native(native("session.compaction.ended", reason="auto", text="old summary"))
        await entered.wait()
        watch = None
        if termination == "stream_eof":
            watch = asyncio.create_task(link._watch_native())
            eof.set()
            await until(lambda: rec.exits)
        else:
            link._proc.alive = False
            link._on_exit(termination == "expected_exit", "synthetic native exit")
        before_entries, before_ends = len(rec.entries), len(rec.turn_ends)
        new_user = link._log.user("new synthetic question after native restart")
        foreign_hint = {"owner": "new-link", "text": "must survive old-link cleanup"}
        link._hint_path.write_text(json.dumps(foreign_hint))
        release.set()
        await until(lambda: not link._tasks)
        if watch:
            await watch
        assert len(rec.entries) == before_entries
        assert len(rec.turn_ends) == before_ends
        entries = link._log.entries()
        new_at = next(i for i, entry in enumerate(entries) if entry.get("uuid") == new_user["uuid"])
        assert not any(entry.get("subtype") == "compact_boundary" for entry in entries[new_at + 1:])
        await link.close()
        assert json.loads(link._hint_path.read_text()) == foreign_hint
    asyncio.run(main())


def test_retired_compaction_cleanup_cannot_delete_a_new_links_hint(tmp_path):
    import json

    async def main():
        link, rec = offline_link(tmp_path)
        server = link._http
        server.admit.clear()
        await link.compact("old hint")
        await server.entered.wait()
        link._proc.alive = False
        link._on_exit(False, "synthetic process exited during admission")
        foreign_hint = {"owner": "new-link", "text": "new hint"}
        link._hint_path.write_text(json.dumps(foreign_hint))
        server.admit.set()
        await until(lambda: not link._tasks)
        assert json.loads(link._hint_path.read_text()) == foreign_hint
        await link.close()
        assert json.loads(link._hint_path.read_text()) == foreign_hint
    asyncio.run(main())


def test_live_turn_helper_reports_native_exit_without_waiting_for_timeout():
    from tests.chat_helpers import LinkRecorder
    from tests.test_chat_opencode_live import turn

    async def main():
        rec = LinkRecorder()

        class ExitedLink:
            async def prompt(self, _text):
                rec.exits.append((False, "sensitive provider detail must not be repeated"))

        with pytest.raises(OpencodeSetupError, match="意外退出") as error:
            await asyncio.wait_for(turn(ExitedLink(), rec, "synthetic input"), .2)
        assert "sensitive" not in str(error.value)
    asyncio.run(main())


def test_a_retired_link_cannot_restart_and_clear_a_new_links_hint(tmp_path):
    import json

    async def main():
        link, _ = offline_link(tmp_path)
        link._proc.alive = False
        link._on_exit(False, "synthetic native exit")
        foreign_hint = {"owner": "new-link", "text": "new hint"}
        link._hint_path.write_text(json.dumps(foreign_hint))
        with pytest.raises(OpencodeSetupError):
            await link.start()
        assert json.loads(link._hint_path.read_text()) == foreign_hint
    asyncio.run(main())
