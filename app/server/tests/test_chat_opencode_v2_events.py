import asyncio
import pytest

from lynch_reader.chat.items import message_items
from lynch_reader.chat.approvals import approvable_path
from tests.chat_helpers import opencode_log, until
from tests.test_chat_opencode_link import make_link
from tests.test_chat_opencode_parity import native


def test_background_compaction_keeps_the_primary_reply_and_its_usage(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    link._prompt_id = "msg_active"
    link._on_native(native("session.compaction.started", reason="auto"))
    link._on_native(native("session.compaction.delta", delta="private summary"))
    link._on_native(native("session.text.delta", assistantMessageID="msg_reply", ordinal=0, delta="answer"))
    link._on_native(native("session.step.ended", assistantMessageID="msg_reply", finish="stop", cost=.01,
                           tokens={"input": 100, "output": 5, "cache": {"read": 10, "write": 0}}))
    assert rec.deltas == ["answer"]
    assert rec.usage[-1][0] == 115


def test_stop_before_compaction_task_starts_never_admits_a_summary(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.compact("one time only")
        await link.interrupt()
        await until(lambda: rec.turn_ends)
        assert rec.turn_ends == [("cancelled", None)]
        assert not any(row.get("http", "").endswith("/compact") for row in opencode_log(tmp_path))
        assert not list(tmp_path.rglob("*.compact-hint.json"))
        await link.close()
    asyncio.run(main())


def test_start_waits_through_the_native_503_readiness_window(tmp_path):
    async def main():
        link, _ = make_link(tmp_path, env={"FAKE_OPENCODE_READY_DELAY": "0.2"})
        try:
            assert (await link.start()).startswith("ses_")
            assert link.alive
        finally:
            await link.close()
    asyncio.run(main())


@pytest.mark.parametrize("error", ["Permission denied: shell", "The user declined this tool call"])
def test_native_v2_rejection_renders_as_a_refused_tool(tmp_path, error):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    link._prompt_id = "msg_active"
    link._on_native(native("session.tool.input.started", assistantMessageID="msg_reply", id="call_1", name="shell"))
    link._on_native(native("session.tool.failed", assistantMessageID="msg_reply", id="call_1",
                           executed=False, error={"type": "PermissionDenied", "message": error}))
    assert message_items(rec.entries[-1])[0]["denied"] is True


def test_native_relative_skill_path_can_reach_the_phone_approval(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    link._prompt_id = "msg_active"
    path = "frameworks/.claude/skills/sediment-framework/probe.md"
    link._on_native(native("session.tool.input.started", assistantMessageID="msg_reply", id="call_1", name="write"))
    link._on_native(native("session.tool.called", assistantMessageID="msg_reply", id="call_1",
                           input={"path": path, "content": "test"}, executed=False))
    link._on_native(native("permission.asked", id="per_1", action="edit", resources=[path],
                           source={"type": "tool", "id": "call_1", "messageID": "msg_reply"}))
    _, _, tool, tool_input = rec.permissions[0]
    assert approvable_path(tool, tool_input, tmp_path) == (tmp_path / path).resolve()


@pytest.mark.parametrize("raw", ["{", "", "[]", "null", '"text"'])
def test_unvalidated_tool_input_can_fail_without_ending_the_research_turn(tmp_path, raw):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    link._prompt_id = "msg_active"
    link._on_native(native("session.tool.input.started", assistantMessageID="msg_reply", id="call_1", name="shell"))
    link._on_native(native("session.tool.input.ended", assistantMessageID="msg_reply", id="call_1", text=raw))
    assert link._tools[("msg_reply", "call_1")]["json"] == raw
    assert link._tools[("msg_reply", "call_1")]["input"] == {}
    link._on_native(native("session.tool.failed", assistantMessageID="msg_reply", id="call_1", executed=False,
                           error={"type": "InvalidToolInput", "message": "native input validation failed"}))
    link._on_native(native("session.text.delta", assistantMessageID="msg_reply", ordinal=0, delta="normal answer"))
    link._on_native(native("session.execution.succeeded"))
    assert not link._broken
    assert rec.deltas == ["normal answer"]
    assert rec.turn_ends == [("end_turn", None)]
    assert message_items(rec.entries[1])[0]["is_error"] is True


def test_only_canonical_called_arguments_are_merged_and_used_for_approval(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    link._prompt_id = "msg_active"
    path = "frameworks/.claude/skills/sediment-framework/probe.md"
    link._on_native(native("session.tool.input.started", assistantMessageID="msg_reply", id="call_1", name="edit"))
    link._on_native(native("session.tool.input.ended", assistantMessageID="msg_reply", id="call_1",
                           text='{"path":"wrong.md","oldString":"raw input"}'))
    assert link._tools[("msg_reply", "call_1")]["input"] == {}
    link._on_native(native("session.tool.called", assistantMessageID="msg_reply", id="call_1", executed=False,
                           input={"path": path, "oldString": "canonical old"}))
    link._on_native(native("session.tool.called", assistantMessageID="msg_reply", id="call_1", executed=False,
                           input={"newString": "canonical new"}))
    link._on_native(native("permission.asked", id="per_1", action="edit", resources=[path],
                           source={"type": "tool", "id": "call_1", "messageID": "msg_reply"}))
    _, _, name, tool_input = rec.permissions[0]
    assert name == "Edit"
    assert tool_input == {"file_path": str((tmp_path / path).resolve()), "old_string": "canonical old",
                          "new_string": "canonical new", "replace_all": False}


@pytest.mark.parametrize("draft", [None, "draft answer"])
def test_text_ended_is_the_authoritative_answer_with_or_without_deltas(tmp_path, draft):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    link._prompt_id = "msg_active"
    link._closed_turn = False
    link._on_native(native("session.text.started", assistantMessageID="msg_reply", ordinal=0))
    if draft is not None:
        link._on_native(native("session.text.delta", assistantMessageID="msg_reply", ordinal=0, delta=draft))
    link._on_native(native("session.text.ended", assistantMessageID="msg_reply", ordinal=0,
                           text="canonical complete answer"))
    link._on_native(native("session.execution.succeeded"))
    answers = [part["text"] for entry in rec.entries if entry["type"] == "assistant"
               for part in entry["message"]["content"] if part["type"] == "text"]
    assert answers == ["canonical complete answer"]
    assert rec.deltas == ([] if draft is None else [draft])


def test_text_ordinals_keep_their_final_content_and_usage_boundary(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    link._prompt_id = "msg_active"
    link._closed_turn = False
    for ordinal, final in ((0, "first canonical block"), (1, "second canonical block")):
        link._on_native(native("session.text.started", assistantMessageID="msg_reply", ordinal=ordinal))
        if ordinal == 0:
            link._on_native(native("session.text.delta", assistantMessageID="msg_reply", ordinal=ordinal,
                                   delta="first draft"))
        link._on_native(native("session.text.ended", assistantMessageID="msg_reply", ordinal=ordinal, text=final))
    link._on_native(native("session.step.ended", assistantMessageID="msg_reply", finish="stop", cost=.01,
                           tokens={"input": 100, "output": 5, "cache": {"read": 0, "write": 0}}))
    link._on_native(native("session.execution.succeeded"))
    answers = [entry["message"] for entry in rec.entries if entry["type"] == "assistant"]
    assert [answer["content"][0]["text"] for answer in answers] == ["first canonical block", "second canonical block"]
    assert "usage" not in answers[0]
    assert answers[1]["usage"] == {"input_tokens": 105}


@pytest.mark.parametrize("first_finished", [False, True])
def test_reused_tool_ids_are_scoped_to_the_assistant_message_and_approval(tmp_path, first_finished):
    link, rec = make_link(tmp_path)
    link._session = "ses_test"
    link._prompt_id = "msg_active"
    first_path = "frameworks/.claude/skills/sediment-framework/first.md"
    second_path = "frameworks/.claude/skills/sediment-framework/second.md"
    for message_id, path in (("msg_first", first_path), ("msg_second", second_path)):
        link._on_native(native("session.tool.input.started", assistantMessageID=message_id,
                               id="call_reused", name="write"))
        link._on_native(native("session.tool.called", assistantMessageID=message_id, id="call_reused",
                               input={"path": path, "content": message_id}, executed=False))
        if first_finished and message_id == "msg_first":
            link._on_native(native("session.tool.success", assistantMessageID=message_id, id="call_reused",
                                   executed=True, content=[{"type": "text", "text": "first result"}]))
    uses = [part for entry in rec.entries if entry["type"] == "assistant"
            for part in entry["message"]["content"] if part["type"] == "tool_use" and part["input"].get("content")]
    assert len(uses) == 2
    assert uses[0]["id"] != uses[1]["id"]
    link._on_native(native("permission.asked", id="per_native", action="edit", resources=[second_path],
                           source={"type": "tool", "id": "call_reused", "messageID": "msg_second"}))
    request_id, tool_id, name, tool_input = rec.permissions[0]
    assert request_id == "per_native"
    assert tool_id == uses[1]["id"]
    assert name == "Write" and tool_input["content"] == "msg_second"
    assert tool_input["file_path"] == str((tmp_path / second_path).resolve())
    link._on_native(native("session.tool.success", assistantMessageID="msg_second", id="call_reused",
                           executed=True, content=[{"type": "text", "text": "second result"}]))
    results = [part for entry in rec.entries if entry["type"] == "user"
               for part in entry["message"]["content"] if part["type"] == "tool_result"]
    assert results[-1]["tool_use_id"] == uses[1]["id"] and results[-1]["content"] == "second result"
    if first_finished:
        assert results[0]["tool_use_id"] == uses[0]["id"] and results[0]["content"] == "first result"
