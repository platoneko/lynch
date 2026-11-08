"""Child reports must not turn the conversation's own thread into a child."""
from __future__ import annotations

import asyncio

import pytest

from tests.chat_helpers import state_of, until
from tests.test_chat_codex_native import active_link, child_event, item_event, start_child
from tests.test_chat_codex_service import codex_service, start


def report_to_parent(link, shape):
    if shape == "subAgentActivity":
        item = {"id": "report", "type": shape, "kind": "interacted",
                "agentThreadId": link._thread, "agentPath": "/root"}
    else:
        item = {"id": "report", "type": shape, "tool": "sendInput", "status": "completed",
                "senderThreadId": "child", "receiverThreadIds": [link._thread], "prompt": "audit ready"}
    child_event(link, "item/completed", item=item)


@pytest.mark.parametrize("shape", ["subAgentActivity", "collabAgentToolCall"])
@pytest.mark.parametrize("command,accepted", [
    ("cat frameworks/AGENTS.md", True),
    ("python3 frameworks/_local/tmp/calculate.py", True),
    ("git -C frameworks push", False),
])
def test_parent_command_approvals_after_child_report(tmp_path, monkeypatch, shape, command, accepted):
    _, link, replies = active_link(tmp_path, monkeypatch)
    start_child(link)
    report_to_parent(link, shape)
    link._on_message({"id": "parent-read", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "parent", "turnId": "turn", "command": command}})
    assert replies == [(("parent-read", {"decision": "accept" if accepted else "decline"}), {})]
    link._on_message({"id": "stale-read", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "parent", "turnId": "old-turn", "command": "cat frameworks/AGENTS.md"}})
    assert replies[-1][1].get("error")


@pytest.mark.parametrize("shape", ["subAgentActivity", "collabAgentToolCall"])
@pytest.mark.parametrize("path,decision", [
    ("frameworks/_local/tmp/research.md", "accept"),
    ("app/server/outside.py", "decline"),
    ("frameworks/.claude/skills/sediment-framework/SKILL.md", "ask"),
])
def test_parent_patch_approvals_after_child_report(tmp_path, monkeypatch, shape, path, decision):
    rec, link, replies = active_link(tmp_path, monkeypatch)
    start_child(link)
    report_to_parent(link, shape)
    item_event(link, {"id": "patch", "type": "fileChange", "status": "inProgress", "changes": [
        {"path": path, "kind": {"type": "add"}, "diff": "+research"}]})
    link._on_message({"id": "parent-patch", "method": "item/fileChange/requestApproval", "params": {
        "threadId": "parent", "turnId": "turn", "itemId": "patch"}})
    if decision == "ask":
        assert replies == [] and len(rec.permissions) == 1
        asyncio.run(link.answer(rec.permissions[0][0], False))
        decision = "decline"
    else:
        assert rec.permissions == []
    assert replies == [(("parent-patch", {"decision": decision}), {})]


@pytest.mark.parametrize("shape", ["subAgentActivity", "collabAgentToolCall"])
def test_parent_final_reply_and_completion_after_child_report(tmp_path, monkeypatch, shape):
    rec, link, _ = active_link(tmp_path, monkeypatch)
    start_child(link)
    report_to_parent(link, shape)
    child_event(link, "item/completed", item={"id": "child-answer", "type": "agentMessage", "text": "audit ready"})
    child_event(link, "turn/completed", turn={"id": "child-turn", "status": "completed"})
    item_event(link, {"id": "answer", "type": "agentMessage", "text": "full research result"}, completed=True)
    end = {"method": "turn/completed", "params": {
        "threadId": "parent", "turn": {"id": "turn", "status": "completed"}}}
    link._on_message(end)
    link._on_message(end)
    assert rec.turn_ends == [("completed", None)]
    entries = link._log.entries()
    answers = [e for e in entries if e.get("message", {}).get("content") == [
        {"type": "text", "text": "full research result"}]]
    assert len(answers) == 1 and not answers[0].get("parent_tool_use_id")
    assert any(e.get("parent_tool_use_id") and e.get("message", {}).get("content") == [
        {"type": "text", "text": "audit ready"}] for e in entries)


def test_thread_started_cannot_register_parent_as_child(tmp_path, monkeypatch):
    rec, link, _ = active_link(tmp_path, monkeypatch)
    start_child(link)
    link._on_message({"method": "thread/started", "params": {
        "thread": {"id": "parent", "parentThreadId": "child"}}})
    item_event(link, {"id": "answer", "type": "agentMessage", "text": "done"}, completed=True)
    link._on_message({"method": "turn/completed", "params": {
        "threadId": "parent", "turn": {"id": "turn", "status": "completed"}}})
    assert rec.turn_ends == [("completed", None)]


@pytest.mark.parametrize("shape", ["subAgentActivity", "collabAgentToolCall"])
def test_service_returns_to_idle_and_keeps_final_reply_after_child_report(tmp_path, shape):
    async def main():
        svc = codex_service(tmp_path)
        try:
            chat = await start(svc, "wait")
            await until(lambda: svc._live[chat.id].proc is not None and svc._live[chat.id].proc._turn is not None)
            link = svc._live[chat.id].proc
            thread, turn = link._thread, link._turn
            link._on_message({"method": "item/started", "params": {
                "threadId": thread, "turnId": turn, "item": {"id": "spawn", "type": "subAgentActivity",
                    "kind": "started", "agentThreadId": "child"}}})
            child_event(link, "turn/started", turn={"id": "child-turn", "status": "inProgress"})
            report_to_parent(link, shape)
            link._on_message({"method": "item/completed", "params": {
                "threadId": thread, "turnId": turn,
                "item": {"id": "answer", "type": "agentMessage", "text": "research finished"}}})
            link._on_message({"method": "turn/completed", "params": {
                "threadId": thread, "turn": {"id": turn, "status": "completed"}}})
            assert state_of(svc, chat.id) == "idle"
            assert svc.list()["running_count"] == 0
            assert any(i.get("text") == "research finished" for i in svc.snapshot(chat.id)["items"])
        finally:
            await svc.shutdown()

    asyncio.run(main())
