from __future__ import annotations

import asyncio
import json

import pytest

from lynch_reader.chat import transcript
from lynch_reader.chat.codex_link import CodexSetupError
from lynch_reader.chat.service import Conflict, Invalid
from tests.chat_helpers import LinkRecorder, next_event, state_of, until
from tests.test_chat_codex_link import open_link, setup
from tests.test_chat_codex_service import codex_service, idle, start, texts
from tests.test_chat_codex_native import active_link, item_event


@pytest.mark.parametrize("mode", ["ok", "before-ack", "stale"])
def test_native_compaction_persists_once_and_resumes_the_original_thread(tmp_path, mode):
    async def main():
        cfg, env = setup(tmp_path, FAKE_CODEX_COMPACT=mode)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            thread = await link.start()
            await link.prompt("first")
            await until(lambda: len(rec.turn_ends) == 1)
            await link.compact()
            await until(lambda: len(rec.turn_ends) == 2)
            entries = transcript.read_entries(cfg.logs / "c1.jsonl")
            history = transcript.history(transcript.current_branch(entries), entries)
            assert [i["kind"] for i in history.items] == ["user", "text", "compact"]
            assert history.items[-1] == {"kind": "compact", "key": entries[-1]["uuid"],
                                        "trigger": "manual", "pre_tokens": 1234, "post_tokens": None}
            assert history.context_tokens is None
            assert not history.unfinished
            if mode == "stale":
                # The denial is sent asynchronously; observe it before tearing down its peer.
                await until(lambda: any(json.loads(line).get("id") == "stale-approval"
                                        for line in (tmp_path / "rpc.jsonl").read_text().splitlines()))
        finally:
            await link.close()
        again = open_link(cfg, env, LinkRecorder(), thread)
        try:
            assert await again.start() == thread
            await again.prompt("continue")
            await until(lambda: transcript.read_entries(cfg.logs / "c1.jsonl")[-1].get("type") == "assistant")
        finally:
            await again.close()
        calls = [json.loads(line) for line in (tmp_path / "rpc.jsonl").read_text().splitlines()]
        assert [c["params"] for c in calls if c.get("method") == "thread/compact/start"] == [{"threadId": thread}]
        assert [c["params"]["input"][0]["text"] for c in calls if c.get("method") == "turn/start"] == ["first", "continue"]
        if mode == "stale":
            approval = next(call for call in calls if call.get("id") == "stale-approval")
            assert "error" in approval
    asyncio.run(main())


def test_service_compaction_events_history_and_continuation_after_restart(tmp_path):
    async def main():
        svc = codex_service(tmp_path)
        chat = await start(svc)
        await idle(svc, chat.id)
        thread = svc.snapshot(chat.id)["conv"]["codex_thread"]
        q = svc.subscribe(chat.id)
        await svc.send(chat.id, "/compact")
        assert (await next_event(q, "state"))["state"] == "compacting"
        item = await next_event(q, "item", lambda item: item["kind"] == "compact")
        assert item["trigger"] == "manual" and item["pre_tokens"] == 1234
        assert (await next_event(q, "context"))["tokens"] is None
        await idle(svc, chat.id)
        assert texts(svc, chat.id) == [("user", "hello"), ("text", "echo: hello"), ("compact", None)]
        assert svc.snapshot(chat.id)["context"]["tokens"] is None
        await svc.shutdown()
        again = codex_service(tmp_path)
        try:
            assert again.snapshot(chat.id)["items"][-1] == item
            assert again.snapshot(chat.id)["context"]["tokens"] is None
            await again.send(chat.id, "next")
            await idle(again, chat.id)
            assert texts(again, chat.id)[-1] == ("text", "echo: next")
            assert again.snapshot(chat.id)["conv"]["codex_thread"] == thread
        finally:
            await again.shutdown()
    asyncio.run(main())


def test_sleeping_codex_can_compact_without_sending_a_user_message(tmp_path):
    async def main():
        svc = codex_service(tmp_path)
        chat = await start(svc)
        await idle(svc, chat.id)
        await svc.shutdown()
        again = codex_service(tmp_path)
        try:
            await again.send(chat.id, "/compact")
            await idle(again, chat.id)
            assert texts(again, chat.id)[-1] == ("compact", None)
            assert [text for kind, text in texts(again, chat.id) if kind == "user"] == ["hello"]
        finally:
            await again.shutdown()
    asyncio.run(main())


def test_compacting_rejects_new_sends_and_can_be_stopped(tmp_path):
    async def main():
        svc = codex_service(tmp_path, env={"FAKE_CODEX_COMPACT": "wait"})
        chat = await start(svc)
        try:
            await idle(svc, chat.id)
            await svc.send(chat.id, "/compact")
            await until(lambda: svc._live[chat.id].proc._turn is not None)
            assert state_of(svc, chat.id) == "compacting"
            await svc.send(chat.id, "next")
            with pytest.raises(Conflict):
                await svc.send(chat.id, "/compact")
            await svc.interrupt(chat.id)
            await idle(svc, chat.id)
            assert texts(svc, chat.id)[-1] == ("notice", "已停止")
            assert not any(kind == "compact" for kind, _ in texts(svc, chat.id))
            for row in svc.snapshot(chat.id)["queued"]:
                svc.dismiss_queued(chat.id, row["id"])
            await svc.send(chat.id, "next")
            await idle(svc, chat.id)
            assert texts(svc, chat.id)[-1] == ("text", "echo: next")
        finally:
            await svc.shutdown()
    asyncio.run(main())


@pytest.mark.parametrize("mode", ["reject", "fail", "crash"])
def test_compaction_failure_is_visible_without_recording_success_or_blocking_next_send(tmp_path, mode):
    async def main():
        svc = codex_service(tmp_path, env={"FAKE_CODEX_COMPACT": mode})
        chat = await start(svc)
        try:
            await idle(svc, chat.id)
            q = svc.subscribe(chat.id)
            if mode == "reject":
                with pytest.raises(Conflict, match="compaction unavailable"):
                    await svc.send(chat.id, "/compact")
            else:
                await svc.send(chat.id, "/compact")
            await until(lambda: state_of(svc, chat.id) in ("idle", "dormant"))
            assert not any(kind == "compact" for kind, _ in texts(svc, chat.id))
            if mode == "crash":
                notice = await next_event(q, "item", lambda item: item["kind"] == "notice")
                assert "compaction crashed" in notice["text"]
            else:
                assert "compaction" in str(texts(svc, chat.id)[-1])
            for row in svc.snapshot(chat.id)["queued"]:
                svc.dismiss_queued(chat.id, row["id"])
            await svc.send(chat.id, "next")
            await idle(svc, chat.id)
            assert texts(svc, chat.id)[-1] == ("text", "echo: next")
        finally:
            await svc.shutdown()
    asyncio.run(main())


def test_compact_start_timeout_closes_uncertain_process(tmp_path):
    async def main():
        cfg, env = setup(tmp_path, FAKE_CODEX_COMPACT="hang")
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            await link.start()
            link._rpc._timeout = 0.1
            with pytest.raises(CodexSetupError, match="thread/compact/start"):
                await link.compact()
            assert not link.alive
            assert "thread/compact/start" in str(rec.entries[-1])
        finally:
            await link.close()
    asyncio.run(main())


def test_stop_before_compaction_ack_closes_process_and_preserves_stop_intent(tmp_path):
    async def main():
        cfg, env = setup(tmp_path, FAKE_CODEX_COMPACT="slow-ack")
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        await link.start()
        compacting = asyncio.create_task(link.compact())
        try:
            await until(lambda: '"thread/compact/start"' in (tmp_path / "rpc.jsonl").read_text())
            await link.interrupt()
            await compacting
            assert not link.alive
            assert rec.turn_ends == [("cancelled", None)]
            assert rec.entries[-1]["message"]["content"].startswith("[Request interrupted")
        finally:
            await link.close()
            await asyncio.gather(compacting, return_exceptions=True)
    asyncio.run(main())


def test_codex_does_not_silently_drop_custom_compaction_instructions(tmp_path):
    async def main():
        svc = codex_service(tmp_path)
        chat = await start(svc)
        try:
            await idle(svc, chat.id)
            await svc.send(chat.id, "/compact 保留估值")
            await idle(svc, chat.id)
            assert texts(svc, chat.id)[-1] == ("compact", None)
        finally:
            await svc.shutdown()
    asyncio.run(main())


def test_auto_compaction_keeps_preceding_answer_before_boundary_and_reports_progress(tmp_path, monkeypatch):
    rec, link, _ = active_link(tmp_path, monkeypatch)
    item_event(link, {"id": "message-before", "type": "agentMessage", "text": "before"}, completed=True)
    link._used = 1234
    compaction = {"id": "auto-compact", "type": "contextCompaction"}
    item_event(link, compaction)
    assert rec.entries[-1] == {"type": "system", "subtype": "status", "status": "compacting"}
    item_event(link, compaction, completed=True)
    item_event(link, compaction, completed=True)
    assert rec.entries[-1] == {"type": "system", "subtype": "status", "status": None}
    entries = transcript.read_entries(tmp_path / "logs/c1.jsonl")
    history = transcript.history(transcript.current_branch(entries), entries)
    assert [(item["kind"], item.get("text")) for item in history.items] == [("text", "before"), ("compact", None)]
    assert history.items[-1]["trigger"] == "auto"
    assert history.context_tokens is None
    assert not rec.turn_ends
