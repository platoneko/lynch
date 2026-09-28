"""Skill edits wait for the phone; every other permission request is refused by the server."""
from __future__ import annotations

import asyncio

import pytest

from lynch_reader.chat.approvals import APPROVABLE_SKILL
from lynch_reader.chat.service import Conflict, Invalid
from tests.chat_helpers import make_service, next_event, state_of, until


def skill_file(tmp_path):
    d = tmp_path / APPROVABLE_SKILL / "references"
    d.mkdir(parents=True)
    (d / "r.md").write_text("alpha\n")
    return d / "r.md"


def tool_items(svc, conv_id):
    return [i for i in svc.snapshot(conv_id)["items"] if i["kind"] == "tool"]


async def pending(svc, conv_id) -> dict:
    """The one approval waiting. Polled from the snapshot: the `approval` event may fire before a test subscribes."""
    await until(lambda: svc.snapshot(conv_id)["approvals"])
    (approval,) = svc.snapshot(conv_id)["approvals"]
    return approval


def test_a_skill_edit_waits_for_the_phone_and_is_applied_on_allow(tmp_path):
    async def main():
        f = skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-edit {f}", "claude-opus-5-5", "medium")
        q = svc.subscribe(conv.id)
        approval = await pending(svc, conv.id)
        assert state_of(svc, conv.id) == "running" and f.read_text() == "alpha\n"
        await svc.answer_approval(conv.id, approval["request_id"], True)
        done = await next_event(q, "approval_done")
        await until(lambda: state_of(svc, conv.id) == "idle")
        tools = tool_items(svc, conv.id)
        await svc.shutdown()
        return approval, done, f.read_text(), tools
    approval, done, text, tools = asyncio.run(main())
    assert approval["path"] == "frameworks/.claude/skills/sediment-framework/references/r.md"
    assert "-alpha" in approval["diff"] and "+beta" in approval["diff"]
    assert "tool_input" not in approval
    assert done == {"request_id": approval["request_id"]}
    assert text == "beta\n"
    assert not tools[-1]["denied"] and not tools[-1]["is_error"]


def test_deny_leaves_the_file_and_marks_the_call_denied(tmp_path):
    async def main():
        f = skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-edit {f}", "claude-opus-5-5", "medium")
        approval = await pending(svc, conv.id)
        await svc.answer_approval(conv.id, approval["request_id"], False)
        await until(lambda: state_of(svc, conv.id) == "idle")
        tools = tool_items(svc, conv.id)
        await svc.shutdown()
        return f.read_text(), tools
    text, tools = asyncio.run(main())
    assert text == "alpha\n"
    assert tools[-1]["denied"]


@pytest.mark.parametrize("text", ["ask-bash ls /", "ask-edit {root}/eval/lib/gordon.py",
                                  "ask-edit {root}/frameworks/.claude/skills/other/SKILL.md",
                                  # relative: claude may have `cd`-ed, so the server cannot tell what it means
                                  "ask-edit frameworks/.claude/skills/sediment-framework/references/r.md"])
def test_other_requests_are_refused_without_asking_the_phone(tmp_path, text):
    # The fake blocks until someone answers; reaching idle with no approval ever listed means the server did.
    async def main():
        skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, text.format(root=tmp_path), "claude-opus-5-5", "medium")
        seen = []
        await until(lambda: seen.extend(svc.snapshot(conv.id)["approvals"]) or state_of(svc, conv.id) == "idle")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return seen, snap
    seen, snap = asyncio.run(main())
    assert seen == [] and snap["approvals"] == []
    assert [i for i in snap["items"] if i["kind"] == "tool"][-1]["denied"]


def test_a_refusal_keeps_claude_codes_own_reason(tmp_path):
    # The agent adapts to the specific rule (e.g. retries `cd … && git` as `git -C frameworks`).
    reason = "Compound commands with cd and git require approval to prevent bare repository attacks"
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-reason {reason}", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        tools = tool_items(svc, conv.id)
        await svc.shutdown()
        return tools
    (tool,) = asyncio.run(main())
    assert tool["denied"] and "requires approval" in tool["output"] and reason in tool["output"]


def test_a_request_that_cannot_be_shown_is_refused(tmp_path, monkeypatch):
    # A bad request must still be answered: claude waits for an answer forever.
    def broken(*args):
        raise ValueError("unreadable")
    monkeypatch.setattr("lynch_reader.chat.service.new_approval", broken)

    async def main():
        f = skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-edit {f}", "claude-opus-5-5", "medium")
        seen = []
        await until(lambda: seen.extend(svc.snapshot(conv.id)["approvals"]) or state_of(svc, conv.id) == "idle")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return seen, snap, f.read_text()
    seen, snap, text = asyncio.run(main())
    assert seen == [] and snap["approvals"] == []
    assert [i for i in snap["items"] if i["kind"] == "tool"][-1]["denied"]
    assert text == "alpha\n"


def test_the_repo_root_symlink_path_is_approvable(tmp_path):
    async def main():
        skill_file(tmp_path)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude/skills").symlink_to("../frameworks/.claude/skills")
        svc, _ = make_service(tmp_path)
        via = tmp_path / ".claude/skills/sediment-framework/references/r.md"
        conv = await svc.create("research", None, f"ask-edit {via}", "claude-opus-5-5", "medium")
        approval = await pending(svc, conv.id)
        await svc.shutdown()
        return approval
    assert asyncio.run(main())["path"] == "frameworks/.claude/skills/sediment-framework/references/r.md"


def test_snapshot_lists_pending_approvals(tmp_path):
    async def main():
        f = skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-edit {f}", "claude-opus-5-5", "medium")
        approval = await pending(svc, conv.id)
        svc.subscribe(conv.id)  # a page reconnecting after the app was killed
        again = svc.snapshot(conv.id)["approvals"]
        await svc.shutdown()
        return approval, again
    approval, again = asyncio.run(main())
    assert again == [approval] and approval["diff"]


def test_interrupt_cancels_the_approval(tmp_path):
    async def main():
        f = skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-edit {f}", "claude-opus-5-5", "medium")
        q = svc.subscribe(conv.id)
        approval = await pending(svc, conv.id)
        await svc.interrupt(conv.id)
        done = await next_event(q, "approval_done")
        await until(lambda: state_of(svc, conv.id) == "idle")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return approval, done, snap, f.read_text()
    approval, done, snap, text = asyncio.run(main())
    assert done == {"request_id": approval["request_id"]} and snap["approvals"] == []
    assert text == "alpha\n"


def test_archive_clears_approvals(tmp_path):
    async def main():
        f = skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-edit {f}", "claude-opus-5-5", "medium")
        q = svc.subscribe(conv.id)
        approval = await pending(svc, conv.id)
        await svc.archive(conv.id)
        done = await next_event(q, "approval_done")
        await svc.shutdown()
        return approval, done
    approval, done = asyncio.run(main())
    assert done == {"request_id": approval["request_id"]}


def test_process_exit_clears_approvals(tmp_path):
    async def main():
        f = skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-edit {f}", "claude-opus-5-5", "medium")
        q = svc.subscribe(conv.id)
        approval = await pending(svc, conv.id)
        svc._live[conv.id].proc._proc.kill()  # the claude process dies while the card is shown
        done = await next_event(q, "approval_done")
        await until(lambda: state_of(svc, conv.id) == "dormant")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return approval, done, snap
    approval, done, snap = asyncio.run(main())
    assert done == {"request_id": approval["request_id"]} and snap["approvals"] == []


def test_a_send_that_respawns_before_the_old_exit_drops_its_approvals(tmp_path):
    # The dead leader's exit is reported only after its group sweep (up to 5 s). A send in that window starts a
    # new process, and the late exit is then stale: the old cards must go when the new process takes over.
    async def main():
        f = skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-edit {f}", "claude-opus-5-5", "medium")
        q = svc.subscribe(conv.id)
        approval = await pending(svc, conv.id)
        old = svc._live[conv.id].proc
        late, report_exit = [], old._on_exit
        old._on_exit = lambda *args: late.append(args)  # hold the exit report until the new process is in
        old._proc.kill()
        await old._proc.wait()
        # "slow" keeps the new turn open, so its `result` cannot clear the old card as a fallback.
        await svc.send(conv.id, "slow")
        await until(lambda: late)
        report_exit(*late[0])
        snap = svc.snapshot(conv.id)
        with pytest.raises(Conflict):  # a tap on the old card must not reach the new process
            await svc.answer_approval(conv.id, approval["request_id"], True)
        done = await next_event(q, "approval_done", timeout=1.0)
        await svc.shutdown()
        return approval, done, snap, f.read_text()
    approval, done, snap, text = asyncio.run(main())
    assert snap["approvals"] == [] and snap["conv"]["state"] == "running"
    assert done == {"request_id": approval["request_id"]}
    assert text == "alpha\n"


def test_a_second_answer_is_a_conflict(tmp_path):
    async def main():
        f = skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-edit {f}", "claude-opus-5-5", "medium")
        rid = (await pending(svc, conv.id))["request_id"]
        # A double tap: two answers racing. Exactly one reaches claude.
        outcomes = await asyncio.gather(svc.answer_approval(conv.id, rid, True),
                                        svc.answer_approval(conv.id, rid, True), return_exceptions=True)
        await until(lambda: state_of(svc, conv.id) == "idle")
        text = f.read_text()
        await svc.shutdown()
        return outcomes, text
    outcomes, text = asyncio.run(main())
    assert sorted(type(o).__name__ for o in outcomes) == ["Conflict", "NoneType"]
    assert text == "beta\n"


def test_answer_validates(tmp_path):
    async def main():
        f = skill_file(tmp_path)
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, f"ask-edit {f}", "claude-opus-5-5", "medium")
        rid = (await pending(svc, conv.id))["request_id"]
        with pytest.raises(Invalid):
            await svc.answer_approval(conv.id, rid, "yes")
        with pytest.raises(Conflict):
            await svc.answer_approval(conv.id, "no-such-request", True)
        await svc.shutdown()
    asyncio.run(main())
