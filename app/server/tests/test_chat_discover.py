from __future__ import annotations

import asyncio
import json
import os
import shutil

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


def test_options_default_to_grok_45_with_500k(tmp_path):
    svc, _ = make_service(tmp_path)
    cache = tmp_path / "home" / ".grok" / "models_cache.json"
    models = json.loads(cache.read_text())["models"]
    models["grok-4.5"] = {"info": {**models["grok-4.6"]["info"], "id": "grok-4.5", "name": "Grok 4.5"}}
    cache.write_text(json.dumps({"models": models}))
    d = svc.options()["discover"]
    assert (d["default_model"], d["default_window"], d["hint"]) == ("grok-4.5", 500000, None)
    assert (d["lookback_days"], d["default_lookback_days"]) == ([1, 2, 3, 7], 2)


def test_options_fall_back_to_the_first_model_and_its_first_window(tmp_path):
    svc, _ = make_service(tmp_path)  # the cache holds grok-4.6 only, with 256k and 500k
    d = svc.options()["discover"]
    assert (d["default_model"], d["default_window"]) == ("grok-4.6", 500000)
    cache = tmp_path / "home" / ".grok" / "models_cache.json"
    models = json.loads(cache.read_text())["models"]
    models["grok-4.6"]["info"]["context_windows"] = [256000]
    cache.write_text(json.dumps({"models": models}))
    assert svc.options()["discover"]["default_window"] == 256000


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
    prompt = next(a.split("=", 1)[1] for a in argv if a.startswith("--single="))
    assert argv[argv.index("--resume") + 1] == conv.grok_session
    assert "最近 2 天" in prompt and snap["conv"]["lookback_days"] == 2
    assert "SKILL-BODY" in prompt and "list:2051493926612484289" in prompt and prompt.endswith("## 用户这一轮说\n看看")
    assert turn["cwd"] == str(tmp_path / "grok-workdir")
    assert turn["grok_env"]["GROK_SANDBOX"] == "read-only"
    assert not [i for i in snap["items"] if i["kind"] == "notice"]  # a normal exit says nothing


def test_each_turn_rereads_the_kol_list_and_adds_up_the_cost(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "one", "grok-4.6", "high", 256000)
        await settled(svc, conv.id)
        write_invest(tmp_path / "invest", list_id="999")
        (tmp_path / "search.md").write_text("# lynch 发现新票：搜索规则与候选格式\n\nEDITED\n", encoding="utf-8")
        await svc.send(conv.id, "two")
        await settled(svc, conv.id)
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return conv, snap
    conv, snap = asyncio.run(main())
    first, second = turns(tmp_path)
    assert second["argv"][second["argv"].index("--resume") + 1] == conv.grok_session
    second_prompt = next(a for a in second["argv"] if a.startswith("--single="))
    assert "list:999" in second_prompt and "EDITED" in second_prompt and "SKILL-BODY" not in second_prompt
    assert snap["conv"]["cost_usd"] == pytest.approx(0.02)
    assert texts(snap)[-2:] == [("user", "two"), ("text", "echo: two")]


def test_a_missing_skill_means_no_session_and_no_chat(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        (tmp_path / "search.md").unlink()
        with pytest.raises(Conflict, match="找不到发现新票的 skill"):
            await svc.create("discover", None, "看看", "grok-4.6", "high", 256000)
        rows = svc.list()["chats"]
        await svc.shutdown()
        return rows
    assert asyncio.run(main()) == []
    assert grok_launches(tmp_path) == []  # not even the ACP handshake ran


def test_a_skill_removed_mid_chat_refuses_the_turn(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "one", "grok-4.6", "high", 256000)
        await settled(svc, conv.id)
        (tmp_path / "search.md").unlink()
        with pytest.raises(Conflict, match="找不到发现新票的 skill"):
            await svc.send(conv.id, "two")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap
    snap = asyncio.run(main())
    assert len(turns(tmp_path)) == 1 and ("user", "two") not in texts(snap)


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


def drained_notices(q):
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return [d["text"] for event, d in events if event == "item" and d["kind"] == "notice"]


@pytest.mark.parametrize("text", ["maxturns", "failed", "plain"])
def test_an_error_result_is_reported_and_still_costs(tmp_path, text):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, text, "grok-4.6", "high", 256000)
        q = svc.subscribe(conv.id)  # safe after create: see test_an_exit_without_a_result_is_reported
        await settled(svc, conv.id)
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return drained_notices(q), snap
    notices, snap = asyncio.run(main())
    assert snap["conv"]["cost_usd"] == pytest.approx(0.01)
    if text == "plain":
        assert notices == []  # a success result says nothing
        return
    [notice] = notices
    assert not notice.startswith("Grok 进程意外退出")  # the exit after any result is normal
    if text == "maxturns":
        assert "40" in notice and "步数上限" in notice and "回看" in notice
    else:
        assert "X search backend unavailable" in notice


def test_a_turn_ends_when_grok_exits_not_at_its_result(tmp_path):
    # A second `--resume` of the same session, or a SIGTERM, while grok is still finishing could lose the turn.
    async def main():
        svc, _ = make_service(tmp_path, idle_timeout_s=0)
        conv = await svc.create("discover", None, "linger", "grok-4.6", "high", 256000)
        await until(lambda: svc.snapshot(conv.id)["conv"]["cost_usd"] is not None)  # its result arrived
        await svc.reap_idle()
        during = state_of(svc, conv.id)
        with pytest.raises(Conflict):
            await svc.send(conv.id, "more")
        # Stop still ends a grok that lingers after answering (it might never exit), without a 已停止.
        await svc.interrupt(conv.id)
        stopped = state_of(svc, conv.id)
        await svc.send(conv.id, "again")
        await settled(svc, conv.id)
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return during, stopped, snap
    during, stopped, snap = asyncio.run(main())
    assert (during, stopped) == ("running", "dormant")
    assert texts(snap) == [("user", "linger"), ("text", "echo: linger"), ("user", "again"), ("text", "echo: again")]
    assert len(turns(tmp_path)) == 2


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
                                                 ("grok-4.6", "high", 1000000), ("grok-4.6", "high", True),
                                                 ("grok-4.6", "high", 256000.0)])
def test_unknown_model_effort_or_window_is_invalid(tmp_path, model, effort, window):
    svc, _ = make_service(tmp_path)
    with pytest.raises(Invalid):
        asyncio.run(svc.create("discover", None, "x", model, effort, window))


def test_the_chosen_lookback_goes_into_every_turn(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "one", "grok-4.6", "high", 256000, 7)
        await settled(svc, conv.id)
        await svc.send(conv.id, "two")
        await settled(svc, conv.id)
        await svc.shutdown()
        return conv
    conv = asyncio.run(main())
    assert conv.lookback_days == 7
    assert all("最近 7 天" in next(a for a in t["argv"] if a.startswith("--single=")) for t in turns(tmp_path))


@pytest.mark.parametrize("days", [5, 0, "2", True, 2.0])
def test_a_lookback_not_offered_is_invalid(tmp_path, days):
    svc, _ = make_service(tmp_path)
    with pytest.raises(Invalid):
        asyncio.run(svc.create("discover", None, "x", "grok-4.6", "high", 256000, days))
    assert grok_launches(tmp_path) == []


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


@pytest.mark.parametrize("blocker", ["file", "readonly"])
def test_an_unusable_workdir_is_a_conflict_on_create(tmp_path, blocker):
    workdir = tmp_path / "grok-workdir"
    if blocker == "file":
        workdir.write_text("not a directory")  # mkdir fails: OSError
    else:
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        workdir.mkdir(mode=0o555)  # `git init` fails: CalledProcessError
    svc, _ = make_service(tmp_path)
    try:
        with pytest.raises(Conflict, match="无法创建 Grok 会话"):
            asyncio.run(svc.create("discover", None, "x", "grok-4.6", "high", 256000))
    finally:
        if workdir.is_dir():
            workdir.chmod(0o755)
    assert turns(tmp_path) == []


def test_an_unusable_workdir_is_a_conflict_on_send(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("discover", None, "one", "grok-4.6", "high", 256000)
        await settled(svc, conv.id)
        workdir = tmp_path / "grok-workdir"
        shutil.rmtree(workdir)
        workdir.write_text("not a directory")
        with pytest.raises(Conflict, match="无法启动 Grok"):
            await svc.send(conv.id, "two")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap
    snap = asyncio.run(main())
    assert texts(snap) == [("user", "one"), ("text", "echo: one")] and snap["conv"]["state"] == "dormant"


def test_only_a_discover_chat_saves_candidates(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("research", None, "hello", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        key = svc.snapshot(conv.id)["items"][-1]["key"]
        with pytest.raises(Invalid, match="发现新票"):
            svc.save_candidates(conv.id, key, ["0700.HK"])
        await svc.shutdown()
    asyncio.run(main())


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
