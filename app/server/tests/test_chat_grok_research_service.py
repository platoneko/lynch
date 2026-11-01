from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lynch_reader.chat.service import Conflict, Invalid
from tests.chat_helpers import grok_launches, make_service, next_event, state_of, until


def acp_log(tmp_path: Path) -> list[dict]:
    log = tmp_path / "grok-acp.jsonl"
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


async def start(svc, text, kind="research", ticker=None, **kw):
    return await svc.create(kind, ticker, text, "grok-4.6", "high", 500000, **kw)


async def idle(svc, conv_id):
    await until(lambda: state_of(svc, conv_id) == "idle")


def test_unnamed_research_chat_defaults_to_grok(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "hi")
        await idle(svc, conv.id)
        row = svc.list()["chats"][0]
        await svc.shutdown()
        return conv, row
    conv, row = asyncio.run(main())
    assert conv.agent == "grok" and row["agent"] == "grok"
    assert (row["model"], row["effort"], row["context_window"]) == ("grok-4.6", "high", 500000)


def test_research_grok_rejects_discover_only_fields(tmp_path):
    svc, _ = make_service(tmp_path)
    with pytest.raises(Invalid):
        asyncio.run(svc.create("research", None, "x", "grok-4.6", "high", 1))


def test_discover_still_rejects_claude(tmp_path):
    svc, _ = make_service(tmp_path)
    with pytest.raises(Invalid):
        asyncio.run(svc.create("discover", None, "x", "claude-opus-5-5", "medium", agent="claude"))


def test_research_grok_resumes_its_session_after_restart(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path, env={"FAKE_GROK_ACP_LOG": str(tmp_path / "grok-acp.jsonl")})
        conv = await start(svc, "one")
        await idle(svc, conv.id)
        sid = svc.list()["chats"][0]["grok_session"]
        await svc.shutdown()
        svc2, _ = make_service(tmp_path, env={"FAKE_GROK_ACP_LOG": str(tmp_path / "grok-acp.jsonl")})
        await svc2.send(conv.id, "two")
        await idle(svc2, conv.id)
        await svc2.shutdown()
        return sid
    sid = asyncio.run(main())
    methods = [r.get("method") for r in acp_log(tmp_path)]
    assert "session/resume" in methods
    assert methods.count("session/new") == 1
    assert any(r.get("method") == "session/resume" and r["params"]["sessionId"] == sid for r in acp_log(tmp_path))


@pytest.mark.parametrize("allow,reply", [(True, "edited"), (False, "（Grok 结束了本轮）")])
def test_research_grok_skill_edit_allow_and_deny(tmp_path, allow, reply):
    async def main():
        svc, _ = make_service(tmp_path)
        skill = tmp_path / "frameworks/.claude/skills/sediment-framework/SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("a\n")
        conv = await start(svc, "skill")
        q = svc.subscribe(conv.id)
        approval = await next_event(q, "approval")
        await svc.answer_approval(conv.id, approval["request_id"], allow)
        await idle(svc, conv.id)
        snap = svc.snapshot(conv.id)
        with pytest.raises(Conflict):
            await svc.answer_approval(conv.id, approval["request_id"], allow)
        await svc.shutdown()
        return approval, snap
    approval, snap = asyncio.run(main())
    assert approval["path"] == "frameworks/.claude/skills/sediment-framework/SKILL.md"
    assert approval["tool"] == "Edit"
    tool = next(i for i in snap["items"] if i["kind"] == "tool")
    assert tool["denied"] is (not allow)
    assert snap["items"][-1]["text"] == reply


def test_research_grok_stop_clears_pending_approval(tmp_path):
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


def test_research_grok_setup_crash_is_a_notice(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path, env={"FAKE_GROK_ACP_EXIT": "1"})
        with pytest.raises(Conflict, match="无法启动 Grok"):
            await start(svc, "hello")
        conv_id = svc._index.all()[0].id
        state = state_of(svc, conv_id)
        await svc.shutdown()
        return state
    assert asyncio.run(main()) == "dormant"


def test_research_grok_link_is_evicted_when_idle(tmp_path):
    clock = {"t": 0.0}

    async def main():
        svc, _ = make_service(tmp_path, monotonic=lambda: clock["t"], max_processes=2)
        grok = await start(svc, "one")
        await idle(svc, grok.id)
        link = svc._live[grok.id].proc
        clock["t"] = 10
        claude = await svc.create("ticker", "AAA.US", "two", "claude-opus-5-5", "medium", agent="claude")
        await idle(svc, claude.id)
        clock["t"] = 20
        third = await svc.create("ticker", "AAA.US", "three", "claude-opus-5-5", "medium", agent="claude")
        await idle(svc, third.id)
        states = state_of(svc, grok.id), state_of(svc, claude.id)
        await svc.shutdown()
        return states, link
    states, link = asyncio.run(main())
    assert states == ("dormant", "idle") and not link.alive


def test_first_framework_write_attaches_ticker(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await start(svc, "writefw")
        await idle(svc, conv.id)
        row = svc.list()["chats"][0]
        await svc.shutdown()
        return row
    assert asyncio.run(main())["ticker"] == "ZZZ.US"


def test_discover_create_still_uses_empty_workdir(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "one", "grok-4.6", "high", 256000)
        await until(lambda: state_of(svc, conv.id) == "dormant")
        await svc.shutdown()
    asyncio.run(main())
    turns = [l for l in grok_launches(tmp_path) if l["argv"][:2] != ["agent", "stdio"]
             and not (len(l["argv"]) >= 2 and l["argv"][-2:] == ["agent", "stdio"])]
    # the -p turn (not the handshake)
    p_turns = [l for l in grok_launches(tmp_path) if any(a.startswith("--single=") for a in l["argv"])]
    assert p_turns
    assert Path(p_turns[0]["cwd"]).name == "grok-workdir"
    assert p_turns[0]["grok_env"]["GROK_SANDBOX"] == "read-only"
