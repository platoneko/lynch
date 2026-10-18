from __future__ import annotations

import asyncio

import pytest

from lynch_reader.chat.service import Conflict, Invalid
from tests.chat_helpers import make_service, next_event, opencode_log, state_of, until

MODEL = "0g/deepseek-v4.1-flash"


def texts(snapshot):
    return [(i["kind"], i.get("text")) for i in snapshot["items"]]


async def start(svc, text, kind="ticker", ticker="AAA.US"):
    return await svc.create(kind, ticker, text, MODEL, "default", agent="opencode")


async def idle(svc, conv_id):
    await until(lambda: state_of(svc, conv_id) == "idle")


def test_opencode_create_streams_a_reply_and_lists_labels(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "hello")
        q = svc.subscribe(conv.id)
        await idle(svc, conv.id)
        snap = svc.snapshot(conv.id)
        row = svc.list()["chats"][0]
        await svc.shutdown()
        return conv, snap, row
    conv, snap, row = asyncio.run(main())
    assert texts(snap) == [("user", "hello"), ("text", "echo: hello")]
    assert (row["agent"], row["agent_label"], row["model_label"], row["effort"]) == (
        "opencode", "opencode", "DeepSeek V4.1 Flash", "default")
    assert row["opencode_session"].startswith("ses_")
    assert snap["context"] == {"tokens": 1234, "window": 1000000}
    assert row["cost_usd"] == 0.01
    assert conv.title == "hello"  # no auto title: the first message stays
    launch = opencode_log(tmp_path)[0]
    assert launch["argv"] == ["acp"] and launch["cwd"] == str(tmp_path)
    assert launch["config"]["agent"]["orchestrator"]["permission"]["edit"]


def test_opencode_deltas_stream_live(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "first")
        await idle(svc, conv.id)
        q = svc.subscribe(conv.id)
        await svc.send(conv.id, "second")
        deltas = [await next_event(q, "delta") for _ in range(2)]
        item = await next_event(q, "item", lambda d: d["kind"] == "text")
        await idle(svc, conv.id)
        await svc.shutdown()
        return deltas, item
    deltas, item = asyncio.run(main())
    assert [d["text"] for d in deltas] == ["echo: ", "second"] and item["text"] == "echo: second"


def test_opencode_resumes_its_session_after_restart(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "one")
        await idle(svc, conv.id)
        sid = svc.list()["chats"][0]["opencode_session"]
        await svc.shutdown()
        svc2, _ = make_service(tmp_path)  # a server restart: same index, nothing live
        before = texts(svc2.snapshot(conv.id))
        await svc2.send(conv.id, "two")
        await idle(svc2, conv.id)
        after = texts(svc2.snapshot(conv.id))
        await svc2.shutdown()
        return sid, before, after
    sid, before, after = asyncio.run(main())
    assert before == [("user", "one"), ("text", "echo: one")]
    assert after == before + [("user", "two"), ("text", "echo: two")]
    calls = [r for r in opencode_log(tmp_path) if r.get("method") in ("session/new", "session/resume")]
    assert [r["method"] for r in calls] == ["session/new", "session/resume"]
    assert calls[1]["params"]["sessionId"] == sid


def test_opencode_stop_clears_pending_approval(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "skill")
        q = svc.subscribe(conv.id)
        approval = await next_event(q, "approval")
        await svc.interrupt(conv.id)
        done = await next_event(q, "approval_done")
        await idle(svc, conv.id)
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return approval, done, snap
    approval, done, snap = asyncio.run(main())
    assert done["request_id"] == approval["request_id"]
    assert snap["approvals"] == [] and snap["items"][-1]["text"] == "已停止"
    tool = next(i for i in snap["items"] if i["kind"] == "tool")
    assert tool["name"] == "Edit" and tool["is_error"] is True


def test_opencode_lost_session_starts_over_with_a_notice(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "one")
        await idle(svc, conv.id)
        sid = svc.list()["chats"][0]["opencode_session"]
        await svc.shutdown()
        (tmp_path / "oc-sessions" / f"{sid}.json").unlink()  # opencode's storage lost it
        svc2, _ = make_service(tmp_path)
        q = svc2.subscribe(conv.id)
        await svc2.send(conv.id, "two")
        notice = await next_event(q, "item", lambda d: d["kind"] == "notice")
        await idle(svc2, conv.id)
        new_sid = svc2.list()["chats"][0]["opencode_session"]
        await svc2.shutdown()
        return sid, new_sid, notice
    sid, new_sid, notice = asyncio.run(main())
    assert new_sid != sid and "之前的内容" in notice["text"]


def test_opencode_setup_crash_is_a_conflict_with_the_reason(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path, env={"FAKE_OPENCODE_EXIT": "1"})
        with pytest.raises(Conflict, match="无法启动 opencode：no provider"):
            await start(svc, "hello")
        conv_id = svc._index.all()[0].id
        state = state_of(svc, conv_id)
        await svc.shutdown()
        return state
    assert asyncio.run(main()) == "dormant"


def test_opencode_link_is_evicted_when_idle(tmp_path):
    clock = {"t": 0.0}

    async def main():
        svc, _ = make_service(tmp_path, monotonic=lambda: clock["t"], max_processes=2)
        oc = await start(svc, "one")
        await idle(svc, oc.id)
        link = svc._live[oc.id].proc
        clock["t"] = 10
        claude = await svc.create("ticker", "AAA.US", "two", "claude-opus-5-5", "medium")
        await idle(svc, claude.id)
        clock["t"] = 20
        third = await svc.create("ticker", "AAA.US", "three", "claude-opus-5-5", "medium")
        await idle(svc, third.id)
        states = state_of(svc, oc.id), state_of(svc, claude.id)
        await svc.shutdown()
        return states, link
    states, link = asyncio.run(main())
    assert states == ("dormant", "idle") and not link.alive


def test_opencode_idle_link_is_reaped(tmp_path):
    clock = {"t": 0.0}

    async def main():
        svc, _ = make_service(tmp_path, monotonic=lambda: clock["t"], idle_timeout_s=100)
        conv = await start(svc, "one")
        await idle(svc, conv.id)
        clock["t"] = 1000
        await svc.reap_idle()
        state = state_of(svc, conv.id)
        await svc.shutdown()
        return state
    assert asyncio.run(main()) == "dormant"


def test_opencode_other_permission_is_refused_without_a_card(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "other")
        q = svc.subscribe(conv.id)
        await idle(svc, conv.id)
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap, q
    snap, q = asyncio.run(main())
    assert snap["approvals"] == []
    assert all(event != "approval" for event, _ in list(q._queue))
    tool = next(i for i in snap["items"] if i["kind"] == "tool")
    assert tool["name"] == "Bash" and tool["denied"] is True


@pytest.mark.parametrize("allow,reply", [(True, "edited"), (False, "（opencode 结束了本轮）")])
def test_opencode_skill_edit_allow_and_deny(tmp_path, allow, reply):
    async def main():
        svc, _ = make_service(tmp_path)
        skill = tmp_path / "frameworks/.claude/skills/sediment-framework/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("a\n")
        conv = await start(svc, "skill")
        q = svc.subscribe(conv.id)
        approval = await next_event(q, "approval")
        await svc.answer_approval(conv.id, approval["request_id"], allow)
        await idle(svc, conv.id)
        snap = svc.snapshot(conv.id)
        with pytest.raises(Conflict):  # answered once only
            await svc.answer_approval(conv.id, approval["request_id"], allow)
        await svc.shutdown()
        return approval, snap
    approval, snap = asyncio.run(main())
    assert approval["path"] == "frameworks/.claude/skills/sediment-framework/SKILL.md"
    assert approval["tool"] == "Edit" and "-a" in approval["diff"] and "+b" in approval["diff"]
    tool = next(i for i in snap["items"] if i["kind"] == "tool")
    assert tool["denied"] is (not allow)
    assert snap["items"][-1]["text"] == reply  # opencode ends the turn on a refusal


def test_opencode_rejects_edit_compact_and_a_second_message_mid_turn(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "slow")
        await until(lambda: state_of(svc, conv.id) == "running")
        with pytest.raises(Conflict, match="回答进行中"):
            await svc.send(conv.id, "more")
        await svc.interrupt(conv.id)
        await idle(svc, conv.id)
        with pytest.raises(Invalid, match="opencode 对话不支持压缩上下文"):
            await svc.send(conv.id, "/compact")
        with pytest.raises(Invalid, match="opencode 对话不支持编辑消息"):
            await svc.edit(conv.id, "x", "y")
        await svc.shutdown()
    asyncio.run(main())


def test_opencode_snapshot_has_no_claude_usage(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        svc.usage = {"five_hour": 0.5, "seven_day": 0.1, "status": "allowed", "resets_at": None}
        conv = await start(svc, "hello")
        await idle(svc, conv.id)
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap
    assert asyncio.run(main())["usage"] is None


def test_research_opencode_chat_joins_the_ticker_it_writes(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "writefw", kind="research", ticker=None)
        await idle(svc, conv.id)
        row = svc.list()["chats"][0]
        await svc.shutdown()
        return row
    assert asyncio.run(main())["ticker"] == "ZZZ.US"


def test_opencode_prompt_error_is_said_once_and_kept(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "first")
        await idle(svc, conv.id)
        q = svc.subscribe(conv.id)
        await svc.send(conv.id, "fail")
        item = await next_event(q, "item", lambda d: d["kind"] == "text")
        await idle(svc, conv.id)
        events = [e for e, d in list(q._queue) if e == "item" and d["kind"] == "notice"]
        await svc.shutdown()
        return item, events
    item, notices = asyncio.run(main())
    assert item["text"] == "opencode 本轮出错：Internal error: Cannot connect to API" and notices == []


def test_opencode_crash_mid_turn_is_a_notice(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "first")
        await idle(svc, conv.id)
        q = svc.subscribe(conv.id)
        await svc.send(conv.id, "crash")
        notice = await next_event(q, "item", lambda d: d["kind"] == "notice")
        await until(lambda: state_of(svc, conv.id) == "dormant")
        await svc.shutdown()
        return notice
    assert asyncio.run(main())["text"] == "opencode 进程意外退出，本轮中断：boom"


@pytest.mark.parametrize("kind,model,effort", [("research", "claude-opus-5-5", "default"),
                                               ("research", MODEL, "high"), ("discover", MODEL, "default")])
def test_create_opencode_validates(tmp_path, kind, model, effort):
    svc, _ = make_service(tmp_path)
    with pytest.raises(Invalid):
        asyncio.run(svc.create(kind, None, "x", model, effort, agent="opencode"))
