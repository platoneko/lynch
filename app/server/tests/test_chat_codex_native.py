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


def child_event(link, method, **params):
    link._on_message({"method": method, "params": {"threadId": "child", "turnId": "child-turn", **params}})


def start_child(link, parent="parent", child="child", via_thread_started=False, kind="started"):
    if via_thread_started:
        link._on_message({"method": "thread/started", "params": {"thread": {
            "id": child, "parentThreadId": parent}}})
    else:
        link._on_message({"method": "item/started", "params": {"threadId": parent,
            "turnId": "turn" if parent == "parent" else "child-turn", "item": {
                "id": "spawn-" + child, "type": "subAgentActivity", "kind": kind, "agentThreadId": child}}})
    link._on_message({"method": "turn/started", "params": {"threadId": child,
        "turn": {"id": "child-turn", "status": "inProgress"}}})


@pytest.mark.parametrize("command,accepted", [("cat frameworks/TEST.US.md", True),
                                           ("cd frameworks && just check", True),
                                           ("git -C frameworks push", False), ("longbridge assets", False)])
@pytest.mark.parametrize("kind", ["started", "interacted"])
def test_child_commands_use_same_guardrails_as_parent(tmp_path, monkeypatch, command, accepted, kind):
    rec, link, replies = active_link(tmp_path, monkeypatch)
    start_child(link, kind=kind)
    link._on_message({"id": "child-approval", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "child", "turnId": "child-turn", "command": command}})
    assert replies == [(("child-approval", {"decision": "accept" if accepted else "decline"}), {})]
    assert rec.turn_ends == []


@pytest.mark.parametrize("path,accepted", [("frameworks/TEST.US.md", True),
    ("frameworks/.claude/skills/sediment-framework/SKILL.md", False), ("app/server/outside.py", False)])
@pytest.mark.parametrize("kind", ["started", "interacted"])
def test_child_patches_check_paths_and_cannot_request_skill_edits(tmp_path, monkeypatch, path, accepted, kind):
    rec, link, replies = active_link(tmp_path, monkeypatch)
    start_child(link, kind=kind)
    child_event(link, "item/started", item={"id": "patch", "type": "fileChange", "changes": [
        {"path": path, "kind": {"type": "update"}, "diff": "+change"}]})
    link._on_message({"id": "child-approval", "method": "item/fileChange/requestApproval", "params": {
        "threadId": "child", "turnId": "child-turn", "itemId": "patch"}})
    assert replies == [(("child-approval", {"decision": "accept" if accepted else "decline"}), {})]
    assert rec.permissions == []


@pytest.mark.parametrize("state", ["unrelated", "stale", "completed", "parent-ended"])
@pytest.mark.parametrize("kind", ["started", "interacted"])
def test_child_approvals_reject_unrelated_or_inactive_turns(tmp_path, monkeypatch, state, kind):
    rec, link, replies = active_link(tmp_path, monkeypatch)
    start_child(link, parent="other" if state == "unrelated" else "parent", kind=kind)
    if state == "completed":
        child_event(link, "turn/completed", turn={"id": "child-turn", "status": "completed"})
    elif state == "parent-ended":
        link._end_turn("completed", None)
    link._on_message({"id": "approval", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "child", "turnId": "old" if state == "stale" else "child-turn", "command": "cat README.md"}})
    assert replies[0][1].get("error")
    if state != "parent-ended":
        assert rec.turn_ends == []


@pytest.mark.parametrize("kind", ["started", "interacted"])
def test_grandchild_is_recognized_and_child_items_do_not_alias_parent_items(tmp_path, monkeypatch, kind):
    rec, link, replies = active_link(tmp_path, monkeypatch)
    start_child(link, kind=kind)
    start_child(link, parent="child", child="grandchild", kind=kind)
    item_event(link, {"id": "patch", "type": "fileChange", "changes": [
        {"path": "frameworks/TEST.US.md", "kind": {"type": "update"}}]})
    child_event(link, "item/started", item={"id": "patch", "type": "fileChange", "changes": [
        {"path": "app/server/outside.py", "kind": {"type": "update"}}]})
    link._on_message({"id": "child-patch", "method": "item/fileChange/requestApproval", "params": {
        "threadId": "child", "turnId": "child-turn", "itemId": "patch"}})
    link._on_message({"id": "grandchild-read", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "grandchild", "turnId": "child-turn", "command": "cat README.md"}})
    assert replies == [(("child-patch", {"decision": "decline"}), {}),
                       (("grandchild-read", {"decision": "accept"}), {})]


def test_child_thread_started_metadata_remains_supported(tmp_path, monkeypatch):
    _, link, replies = active_link(tmp_path, monkeypatch)
    start_child(link, via_thread_started=True)
    link._on_message({"id": "read", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "child", "turnId": "child-turn", "command": "cat README.md"}})
    assert replies == [(("read", {"decision": "accept"}), {})]


@pytest.mark.parametrize("kind", ["started", "interacted"])
def test_child_creation_and_approval_wait_for_parent_turn_start_ack(tmp_path, monkeypatch, kind):
    _, link, replies = active_link(tmp_path, monkeypatch)
    link._starting = True
    start_child(link, kind=kind)
    link._on_message({"id": "read", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "child", "turnId": "child-turn", "command": "cat README.md"}})
    assert replies == []
    link._starting = False
    deferred, link._deferred = link._deferred, []
    for message in deferred:
        link._on_message(message)
    assert replies == [(("read", {"decision": "accept"}), {})]


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
