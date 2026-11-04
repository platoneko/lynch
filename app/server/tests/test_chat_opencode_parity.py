from __future__ import annotations

import asyncio
import json

import pytest

from lynch_reader.chat.opencode_link import OpencodeSetupError
from tests.chat_helpers import opencode_log, until
from tests.test_chat_opencode_link import make_link, oc_conv


def test_tools_are_visible_before_completion_and_inputs_update(tmp_path):
    link, rec = make_link(tmp_path)
    link._on_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "t1",
            "title": "bash",
            "status": "pending",
        }
    )
    assert rec.entries[-1]["message"]["content"][0] == {
        "type": "tool_use",
        "id": "t1",
        "name": "Bash",
        "input": {},
    }
    link._on_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "status": "in_progress",
            "rawInput": {"command": "echo hi"},
        }
    )
    assert rec.entries[-1]["message"]["content"][0]["input"] == {"command": "echo hi"}
    assert rec.entries[-1]["message"]["content"][0]["id"] == "t1"


def test_native_prompt_identity_and_fork_excludes_edited_message(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        old = await link.start()
        await link.prompt("retain amber")
        await until(lambda: len(rec.turn_ends) == 1)
        await link.prompt("forget blue")
        await until(lambda: len(rec.turn_ends) == 2)
        users = [
            e
            for e in rec.entries
            if e["type"] == "user" and isinstance(e["message"]["content"], str)
        ]
        assert link._log.native_id(users[-1]["uuid"])
        sentinel = tmp_path / "keep.txt"
        sentinel.write_text("must survive")
        new = await link.rewind(users[-1]["uuid"])
        assert new != old and link._session == new
        await link.prompt("new ending")
        await until(lambda: len(rec.turn_ends) == 3)
        await link.close()
        native = json.loads((tmp_path / "oc-sessions" / f"{new}.json").read_text())
        text = json.dumps(native)
        assert (
            "retain amber" in text
            and "forget blue" not in text
            and "new ending" in text
        )
        assert sentinel.read_text() == "must survive"

    asyncio.run(main())


def test_failed_fork_keeps_original_session(tmp_path):
    async def main():
        link, rec = make_link(tmp_path, env={"FAKE_OPENCODE_FORK_FAIL": "1"})
        old = await link.start()
        await link.prompt("hi")
        await until(lambda: rec.turn_ends)
        user = next(e for e in rec.entries if e["type"] == "user")
        with pytest.raises(OpencodeSetupError):
            await link.rewind(user["uuid"])
        assert link._session == old
        await link.close()

    asyncio.run(main())


def test_native_compaction_uses_hint_and_restores_process(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        states = []
        link._events.on_state = states.append
        sid = await link.start()
        await link.prompt("retain number 7391")
        await until(lambda: rec.turn_ends)
        await link.compact("务必保留数字 7391")
        await until(lambda: len(rec.turn_ends) == 2)
        assert link._session == sid and link.alive
        boundaries = [e for e in rec.entries if e.get("subtype") == "compact_boundary"]
        assert (
            len(boundaries) == 1
            and boundaries[0]["compactMetadata"]["trigger"] == "manual"
        )
        assert "compacting" in states
        launches = [r for r in opencode_log(tmp_path) if "argv" in r]
        prompts = [
            (r.get("config") or {})
            .get("agent", {})
            .get("compaction", {})
            .get("prompt", "")
            for r in launches
        ]
        assert any("务必保留数字 7391" in p for p in prompts)
        assert "务必保留数字 7391" not in prompts[-1]
        await link.close()

    asyncio.run(main())


def test_title_reads_native_metadata_without_prompting(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("hi")
        await until(lambda: rec.turn_ends)
        title = await link.generate_title("ignored title description")
        await link.close()
        assert title == "原生自动标题"
        assert (
            len(
                [
                    r
                    for r in opencode_log(tmp_path)
                    if r.get("method") == "session/prompt"
                ]
            )
            == 1
        )

    asyncio.run(main())


def test_lost_native_session_fails_without_creating_replacement(tmp_path):
    async def main():
        link, _ = make_link(tmp_path, c=oc_conv(opencode_session="ses_gone"))
        with pytest.raises(OpencodeSetupError, match="session not found"):
            await link.start()
        assert not link.alive
        assert not any(r.get("method") == "session/new" for r in opencode_log(tmp_path))

    asyncio.run(main())


def test_compaction_events_are_deduplicated_after_process_recreation(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    start = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "type": "compaction",
                "sessionID": "ses_test",
                "messageID": "msg_compact",
                "auto": True,
            }
        },
    }
    end = {"type": "session.compacted", "properties": {"sessionID": "ses_test"}}
    link._on_native(start)
    link._on_native(end)
    link._on_native(end)
    assert len(rec.entries) == 1
    resumed, again = make_link(tmp_path)
    resumed._session = "ses_test"
    resumed._on_native(start)
    resumed._on_native(end)
    assert again.entries == []


def test_auto_compaction_summary_is_not_rendered_as_a_research_answer(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    link._on_native(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "compaction",
                    "sessionID": "ses_test",
                    "messageID": "msg_compact",
                    "auto": True,
                }
            },
        }
    )
    link._on_update(
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "summary",
            "content": {"type": "text", "text": "hidden summary"},
        }
    )
    assert rec.deltas == []


def test_native_retry_and_late_title_only_apply_to_owned_session(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    retries = []
    titles = []
    link._events.on_retry = retries.append
    link._events.on_title = titles.append
    link._on_native(
        {
            "type": "session.status",
            "properties": {
                "sessionID": "ses_foreign",
                "status": {"type": "retry", "message": "foreign"},
            },
        }
    )
    link._on_native(
        {
            "type": "session.status",
            "properties": {
                "sessionID": "ses_test",
                "status": {
                    "type": "retry",
                    "message": "rate limited",
                    "attempt": 1,
                    "next": 123,
                },
            },
        }
    )
    link._on_native(
        {
            "type": "session.status",
            "properties": {"sessionID": "ses_test", "status": {"type": "busy"}},
        }
    )
    link._on_native(
        {
            "type": "session.updated",
            "properties": {"info": {"id": "ses_test", "title": "原生晚到标题"}},
        }
    )
    assert retries == ["rate limited", None] and titles == ["原生晚到标题"]


def test_ambiguous_legacy_message_is_rejected_without_fork(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        original = await link.start()
        for i in range(2):
            await link.prompt("same message")
            await until(lambda: len(rec.turn_ends) == i + 1)
        unbound = link._log.user("same message")
        with pytest.raises(OpencodeSetupError, match="唯一匹配"):
            await link.rewind(unbound["uuid"])
        assert link._session == original
        await link.close()

    asyncio.run(main())


def test_compaction_returns_promptly_and_stop_cancels_native_summary(tmp_path):
    async def main():
        link, rec = make_link(tmp_path, env={"FAKE_OPENCODE_COMPACT_SLOW": "1"})
        await link.start()
        await link.prompt("hi")
        await until(lambda: len(rec.turn_ends) == 1)
        await asyncio.wait_for(link.compact(), 0.3)
        await until(
            lambda: any(
                "summarize" in row.get("http", "") for row in opencode_log(tmp_path)
            )
        )
        await link.interrupt()
        await until(lambda: len(rec.turn_ends) == 2)
        assert rec.turn_ends[-1] == ("cancelled", None)
        assert not any(
            entry.get("subtype") == "compact_boundary" for entry in rec.entries
        )
        await link.close()

    asyncio.run(main())
