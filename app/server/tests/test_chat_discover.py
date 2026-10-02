from __future__ import annotations

import asyncio

import pytest

from lynch_reader.chat.service import Conflict, Invalid
from tests.chat_helpers import (grok_launches, grok_session_file, make_service, next_event, state_of, until,
                                write_invest)


def texts(snapshot):
    return [(i["kind"], i.get("text")) for i in snapshot["items"]]


def turns(tmp_path):
    return [l for l in grok_launches(tmp_path) if l["argv"][:2] != ["agent", "stdio"]]


async def settled(svc, conv_id):
    await until(lambda: state_of(svc, conv_id) == "dormant" and not svc.snapshot(conv_id)["partial"])


def test_options_list_search_capable_grok_models(tmp_path):
    svc, _ = make_service(tmp_path)
    d = svc.options()["discover"]
    assert [m["id"] for m in d["models"]] == ["grok-4.6"]
    assert (d["default_model"], d["default_window"], d["hint"]) == ("grok-4.6", 256000, None)


def test_create_makes_a_session_then_runs_a_turn(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "看看", "grok-4.6", "high", 500000)
        await settled(svc, conv.id)
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return conv, snap
    conv, snap = asyncio.run(main())
    assert grok_session_file(tmp_path, conv.grok_session)["window"] == 500000
    assert texts(snap) == [("user", "看看"), ("text", "echo: 看看")]
    assert snap["context"] == {"tokens": 1000, "window": 500000}
    assert snap["usage"] is None and snap["conv"]["cost_usd"] == pytest.approx(0.01)
    assert snap["conv"]["kind"] == "discover" and snap["conv"]["title"] == "看看"
    [turn] = turns(tmp_path)
    argv = turn["argv"]
    assert argv[argv.index("--resume") + 1] == conv.grok_session and "--single=看看" in argv
    assert "@alice" in argv[argv.index("--rules") + 1]
    assert turn["cwd"] == str(tmp_path / "grok-workdir")
    assert turn["grok_env"]["GROK_SANDBOX"] == "read-only"
    assert not [i for i in snap["items"] if i["kind"] == "notice"]  # a normal exit says nothing


def test_each_turn_rereads_the_kol_list_and_adds_up_the_cost(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "one", "grok-4.6", "high", 256000)
        await settled(svc, conv.id)
        write_invest(tmp_path / "invest", handles=("carol",))
        await svc.send(conv.id, "two")
        await settled(svc, conv.id)
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return conv, snap
    conv, snap = asyncio.run(main())
    first, second = turns(tmp_path)
    assert second["argv"][second["argv"].index("--resume") + 1] == conv.grok_session
    assert "@carol" in second["argv"][second["argv"].index("--rules") + 1]
    assert snap["conv"]["cost_usd"] == pytest.approx(0.02)
    assert texts(snap)[-2:] == [("user", "two"), ("text", "echo: two")]


def test_restart_resumes_the_same_grok_session(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "one", "grok-4.6", "high", 256000)
        await settled(svc, conv.id)
        await svc.shutdown()
        svc2, _ = make_service(tmp_path)  # same index and logs: a server restart
        assert texts(svc2.snapshot(conv.id)) == [("user", "one"), ("text", "echo: one")]
        await svc2.send(conv.id, "two")
        await settled(svc2, conv.id)
        snap = svc2.snapshot(conv.id)
        await svc2.shutdown()
        return conv, snap
    conv, snap = asyncio.run(main())
    assert all(t["argv"][t["argv"].index("--resume") + 1] == conv.grok_session for t in turns(tmp_path))
    assert texts(snap) == [("user", "one"), ("text", "echo: one"), ("user", "two"), ("text", "echo: two")]


def test_busy_send_is_refused_and_stop_marks_the_turn(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "slow", "grok-4.6", "high", 256000)
        await until(lambda: any(i["kind"] == "tool" for i in svc.snapshot(conv.id)["items"]))
        with pytest.raises(Conflict):
            await svc.send(conv.id, "more")
        q = svc.subscribe(conv.id)
        await svc.interrupt(conv.id)
        await next_event(q, "item", lambda d: d.get("text") == "已停止")
        assert state_of(svc, conv.id) == "dormant"
        await svc.send(conv.id, "again")
        await settled(svc, conv.id)
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap
    snap = asyncio.run(main())
    assert texts(snap)[-3:] == [("notice", "已停止"), ("user", "again"), ("text", "echo: again")]


@pytest.mark.parametrize("text", ["crash", "noresult"])
def test_an_exit_without_a_result_is_reported(tmp_path, text):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, text, "grok-4.6", "high", 256000)
        # The exit notice is broadcast, not logged. create returns without yielding once grok starts,
        # so its exit cannot have been reported before this subscription.
        q = svc.subscribe(conv.id)
        notice = await next_event(q, "item", lambda d: d["kind"] == "notice")
        state = state_of(svc, conv.id)
        await svc.shutdown()
        return notice, state
    notice, state = asyncio.run(main())
    assert notice["text"].startswith("Grok 进程意外退出") and state == "dormant"


def test_x_search_shows_as_a_finished_tool(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "search", "grok-4.6", "high", 256000)
        await settled(svc, conv.id)
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap
    [tool] = [i for i in asyncio.run(main())["items"] if i["kind"] == "tool"]
    assert tool["name"] == "X search:" and tool["output"] and tool["is_error"] is False and tool["denied"] is False


def test_edit_and_compact_are_refused(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "one", "grok-4.6", "high", 256000)
        await settled(svc, conv.id)
        user_uuid = svc.snapshot(conv.id)["items"][0]["uuid"]
        with pytest.raises(Invalid):
            await svc.edit(conv.id, user_uuid, "x")
        with pytest.raises(Invalid):
            await svc.send(conv.id, "/compact")
        await svc.shutdown()
    asyncio.run(main())


@pytest.mark.parametrize("model,effort,window", [("grok-secret", "high", 256000), ("grok-4.6", "max", 256000),
                                                 ("grok-4.6", "high", 1000000), ("grok-4.6", "high", True)])
def test_unknown_model_effort_or_window_is_invalid(tmp_path, model, effort, window):
    svc, _ = make_service(tmp_path)
    with pytest.raises(Invalid):
        asyncio.run(svc.create("discover", None, "x", model, effort, window))


def test_no_kol_list_means_no_session(tmp_path):
    write_invest(tmp_path / "invest", list_id=None, handles=())
    svc, _ = make_service(tmp_path)
    with pytest.raises(Conflict, match="KOL"):
        asyncio.run(svc.create("discover", None, "x", "grok-4.6", "high", 256000))
    assert grok_launches(tmp_path) == []


def test_session_setup_failure_is_a_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_GROK_ACP_EXIT", "1")
    svc, _ = make_service(tmp_path)
    with pytest.raises(Conflict, match="无法创建 Grok 会话"):
        asyncio.run(svc.create("discover", None, "x", "grok-4.6", "high", 256000))


def test_save_candidates_from_the_reply(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "candidates", "grok-4.6", "high", 256000)
        await settled(svc, conv.id)
        [item] = [i for i in svc.snapshot(conv.id)["items"] if i.get("candidates")]
        assert [c["ticker"] for c in item["candidates"]] == ["0700.HK", "AXTI.US"]
        saved = svc.save_candidates(conv.id, item["key"], ["0700.HK"])
        with pytest.raises(Conflict):
            svc.save_candidates(conv.id, "no-such-key", ["0700.HK"])
        with pytest.raises(Conflict):
            svc.save_candidates(conv.id, item["key"], ["MSFT.US"])
        with pytest.raises(Invalid):
            svc.save_candidates(conv.id, item["key"], "0700.HK")
        rows = svc.candidates.all()
        await svc.shutdown()
        return conv, saved, rows
    conv, saved, rows = asyncio.run(main())
    assert saved == ["0700.HK"]
    assert [(r["ticker"], r["name"], r["sources"][0]["conv_id"]) for r in rows] == [("0700.HK", "腾讯控股", conv.id)]


def test_claude_chats_are_unchanged_by_discover(tmp_path):
    async def main():
        svc, log = make_service(tmp_path)
        conv = await svc.create("research", None, "hello", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap
    snap = asyncio.run(main())
    assert texts(snap) == [("user", "hello"), ("text", "echo: hello")] and snap["usage"] is not None
    assert grok_launches(tmp_path) == []
