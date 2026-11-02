"""Native Codex items retain the information needed by the transcript and phone."""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.chat_helpers import LinkRecorder
from tests.test_chat_codex_link import open_link, setup


def active_link(tmp_path, monkeypatch):
    cfg, env = setup(tmp_path)
    rec = LinkRecorder()
    link = open_link(cfg, env, rec)
    link._thread, link._turn, link._active = "parent", "turn", True
    replies = []
    monkeypatch.setattr(link._rpc, "reply", lambda *args, **kwargs: replies.append((args, kwargs)))
    return rec, link, replies


def item_event(link, item, completed=False):
    link._on_message({"method": "item/completed" if completed else "item/started", "params": {
        "threadId": "parent", "turnId": "turn", "item": item}})


def approve_patch(link, changes):
    item_event(link, {"id": "patch", "type": "fileChange", "status": "inProgress", "changes": changes})
    link._on_message({"id": "approval", "method": "item/fileChange/requestApproval", "params": {
        "threadId": "parent", "turnId": "turn", "itemId": "patch"}})


def test_native_agent_start_and_completion_keep_thread_context_and_result(tmp_path, monkeypatch):
    rec, link, _ = active_link(tmp_path, monkeypatch)
    item = {"id": "agent", "type": "collabAgentToolCall", "tool": "spawnAgent", "status": "inProgress",
            "senderThreadId": "parent", "receiverThreadIds": ["child"], "prompt": "核对测试", "agentsStates": {}}
    item_event(link, item)
    states = {"child": {"status": "completed", "message": "测试已核对"}}
    item_event(link, {**item, "status": "completed", "agentsStates": states}, completed=True)
    item_event(link, {**item, "status": "completed", "agentsStates": states}, completed=True)

    blocks = [b for e in rec.entries for b in e["message"]["content"]]
    assert len(blocks) == 2
    assert blocks[0] == {"type": "tool_use", "id": "agent", "name": "Agent", "input": {
        "prompt": "核对测试", "tool": "spawnAgent", "senderThreadId": "parent", "receiverThreadIds": ["child"]}}
    assert blocks[1]["tool_use_id"] == "agent" and not blocks[1]["is_error"]
    assert json.loads(blocks[1]["content"]) == states


@pytest.mark.parametrize("move_key", ["move_path", "movePath"])
@pytest.mark.parametrize("source,destination,approval_path", [
    ("frameworks/from.md", "frameworks/.claude/skills/sediment-framework/to.md",
     "frameworks/.claude/skills/sediment-framework/to.md"),
    ("frameworks/.claude/skills/sediment-framework/from.md", "frameworks/to.md",
     "frameworks/.claude/skills/sediment-framework/from.md"),
])
def test_skill_moves_show_both_paths_and_operation_before_entire_patch_can_be_declined(
        tmp_path, monkeypatch, move_key, source, destination, approval_path):
    rec, link, replies = active_link(tmp_path, monkeypatch)
    approve_patch(link, [{"path": source, "kind": {"type": "update", move_key: destination}, "diff": "+changed"}])
    assert len(rec.permissions) == 1
    assert replies == []
    request, tool_id, name, inputs = rec.permissions[0]
    assert tool_id == "patch:0" and name == "Write"
    assert inputs["file_path"] == str(tmp_path / approval_path)
    assert "move" in inputs["_codex_diff"]
    assert str(tmp_path / source) in inputs["_codex_diff"]
    assert str(tmp_path / destination) in inputs["_codex_diff"]
    assert "+changed" in inputs["_codex_diff"]
    recorded = rec.entries[0]["message"]["content"][0]["input"]
    assert recorded["file_path"] == str(tmp_path / source)
    assert recorded["move_path"] == str(tmp_path / destination)
    assert recorded["operation"] == "move"
    asyncio.run(link.answer(request, False))
    assert replies == [(("approval", {"decision": "decline"}), {})]


@pytest.mark.parametrize("last_allow", [True, False])
def test_multi_file_skill_patch_waits_for_every_allow_and_any_decline_rejects_all(tmp_path, monkeypatch, last_allow):
    rec, link, replies = active_link(tmp_path, monkeypatch)
    approve_patch(link, [{"path": f"frameworks/.claude/skills/sediment-framework/{name}.md",
                          "kind": {"type": "update"}, "diff": "+changed"} for name in ("a", "b", "c")])
    assert len(rec.permissions) == 3
    requests = [permission[0] for permission in rec.permissions]
    asyncio.run(link.answer(requests[0], True))
    assert replies == []
    asyncio.run(link.answer(requests[1], last_allow))
    if last_allow:
        assert replies == []
    asyncio.run(link.answer(requests[2], True))
    assert replies == [(("approval", {"decision": "accept" if last_allow else "decline"}), {})]
    assert not link._asks


@pytest.mark.parametrize("bad_change", [
    {"path": "app/server/outside.py", "kind": {"type": "update"}},
    {"path": "frameworks/from.md", "kind": {"type": "update", "movePath": "app/server/outside.py"}},
    {"path": "frameworks/from.md", "kind": {"type": "update", "move_path": 42}},
    {"path": "frameworks/from.md", "kind": {"type": "update", "move_path": ""}},
    {"path": "frameworks/from.md", "kind": "invalid"},
    {"kind": {"type": "update"}},
    {"path": "frameworks/from.md", "kind": {"type": "update", "move_path": "frameworks/a.md",
                                             "movePath": "app/server/outside.py"}},
])
def test_entire_patch_is_denied_before_phone_approval_for_invalid_or_outside_changes(tmp_path, monkeypatch, bad_change):
    rec, link, replies = active_link(tmp_path, monkeypatch)
    approve_patch(link, [{"path": "frameworks/.claude/skills/sediment-framework/good.md",
                          "kind": {"type": "update"}, "diff": "+good"}, bad_change])
    assert rec.permissions == []
    assert replies == [(("approval", {"decision": "decline"}), {})]
