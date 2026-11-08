from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from lynch_reader.chat.codex import CodexConfig, CodexModel
from lynch_reader.chat.codex_link import CodexSetupError
from lynch_reader.chat.index import ChatIndex
from lynch_reader.chat.routes import chat_lifespan, chat_routes
from lynch_reader.chat.service import Conflict, Invalid
from tests.chat_helpers import conv, make_service, next_event, state_of, until


def codex_service(tmp_path, **kwargs):
    cfg = CodexConfig((sys.executable, str(__file__).replace("test_chat_codex_service.py", "fake_codex.py")),
                      tmp_path, tmp_path / "codex-logs")
    env = {"FAKE_CODEX_SESSIONS": str(tmp_path / "codex-sessions"),
           "FAKE_CODEX_LOG": str(tmp_path / "codex-rpc.jsonl"), **kwargs.pop("env", {})}
    return make_service(tmp_path, codex=cfg, env=env, **kwargs)[0]


def models():
    return [CodexModel("gpt-other", "Other GPT", ("low",), "low", False),
            CodexModel("gpt-selected", "Selected GPT", ("medium", "high"), "high", True)]


def test_codex_catalog_is_shared_cached_and_refreshes_after_five_minutes(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    calls = []

    async def discover(cfg, env):
        calls.append(cfg)
        await asyncio.sleep(0.01)
        return models() if len(calls) == 1 else models()[:1]

    monkeypatch.setattr("lynch_reader.chat.service.discover_models", discover)

    async def main():
        svc = codex_service(tmp_path, monotonic=lambda: clock["t"])
        await asyncio.gather(*(svc.refresh_codex_models() for _ in range(3)))
        initial = svc.options()
        clock["t"] = 299
        await svc.refresh_codex_models()
        assert len(calls) == 1
        clock["t"] = 300
        await svc.refresh_codex_models()
        return initial, svc.options()

    initial, refreshed = asyncio.run(main())
    assert initial["default_agent"] == "codex"
    assert {"id": "grok", "label": "Grok"} in initial["agents"]
    assert initial["grok"]["default_model"] == "grok-4.6"
    assert {"id": "codex", "label": "Codex"} in initial["agents"]
    assert initial["codex"] == {"models": [
        {"id": "gpt-other", "label": "Other GPT", "efforts": ["low"], "default_effort": "low"},
        {"id": "gpt-selected", "label": "Selected GPT", "efforts": ["medium", "high"], "default_effort": "high"}],
        "default_model": "gpt-selected", "hint": None}
    assert refreshed["codex"]["default_model"] == "gpt-other"


@pytest.mark.parametrize("kind,ticker", [("research", None), ("ticker", "AAA.US")])
def test_new_chats_prefer_terra_xhigh_over_codex_account_defaults(tmp_path, monkeypatch, kind, ticker):
    async def discover(cfg, env):
        return [*models(), CodexModel("gpt-6.1-terra", "GPT 6.1 Terra", ("medium", "high", "xhigh"), "high")]

    monkeypatch.setattr("lynch_reader.chat.service.discover_models", discover)
    svc = codex_service(tmp_path)
    (tmp_path / "frameworks" / "AAA.US.md").write_text("# AAA\n")
    with TestClient(Starlette(routes=chat_routes(svc, tmp_path / "frameworks"), lifespan=chat_lifespan(svc))) as client:
        catalog = client.get("/api/chats/codex/options").json()
        assert catalog["default_model"] == "gpt-6.1-terra"
        assert next(m for m in catalog["models"] if m["id"] == "gpt-6.1-terra")["default_effort"] == "xhigh"
        response = client.post("/api/chats", json={"kind": kind, "ticker": ticker, "text": "hi"})
        response.raise_for_status()
        row = svc.snapshot(response.json()["id"])["conv"]
        assert (row["agent"], row["model"], row["effort"]) == ("codex", "gpt-6.1-terra", "xhigh")
        explicit = client.post("/api/chats", json={"kind": kind, "ticker": ticker, "text": "hi",
                                                  "agent": "codex", "model": "gpt-selected", "effort": "high"})
        explicit.raise_for_status()
        row = svc.snapshot(explicit.json()["id"])["conv"]
        assert (row["model"], row["effort"]) == ("gpt-selected", "high")


@pytest.mark.parametrize("error", [CodexSetupError("请在终端运行 codex login"), OSError("codex missing")])
def test_codex_catalog_failure_is_visible_and_does_not_break_other_options(tmp_path, monkeypatch, error):
    async def discover(cfg, env):
        raise error

    monkeypatch.setattr("lynch_reader.chat.service.discover_models", discover)
    svc = codex_service(tmp_path)
    with TestClient(Starlette(routes=chat_routes(svc, tmp_path / "frameworks"), lifespan=chat_lifespan(svc))) as client:
        codex = client.get("/api/chats/codex/options")
        assert codex.status_code == 200
        assert codex.json()["models"] == [] and str(error) in codex.json()["hint"]
        opts = client.get("/api/chats/options")
        assert opts.status_code == 200 and opts.json()["models"]
        assert opts.json()["grok"]["models"]
        response = client.post("/api/chats", json={"kind": "research", "text": "hi", "agent": "codex"})
        assert response.status_code == 409 and str(error) in response.json()["error"]
    assert svc._index.all() == []


def test_general_options_never_starts_or_waits_for_codex_discovery(tmp_path, monkeypatch):
    async def main():
        started, release = asyncio.Event(), asyncio.Event()

        async def discover(cfg, env):
            started.set()
            await release.wait()
            return models()

        monkeypatch.setattr("lynch_reader.chat.service.discover_models", discover)
        svc = codex_service(tmp_path)
        app = Starlette(routes=chat_routes(svc, tmp_path / "frameworks"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            general = await asyncio.wait_for(client.get("/api/chats/options"), 0.5)
            assert general.status_code == 200 and general.json()["grok"]["models"]
            assert not started.is_set()
            assert not (tmp_path / "codex-rpc.jsonl").exists()
            probe = asyncio.create_task(client.get("/api/chats/codex/options"))
            try:
                await asyncio.wait_for(started.wait(), 0.5)
                general = await asyncio.wait_for(client.get("/api/chats/options"), 0.5)
                assert general.status_code == 200 and general.json()["models"]
                assert not probe.done()
            finally:
                release.set()
                catalog = await asyncio.wait_for(probe, 1)
            assert catalog.status_code == 200
            assert catalog.json() == {"models": [
                {"id": "gpt-other", "label": "Other GPT", "efforts": ["low"], "default_effort": "low"},
                {"id": "gpt-selected", "label": "Selected GPT", "efforts": ["medium", "high"],
                 "default_effort": "high"}], "default_model": "gpt-selected", "hint": None}

    asyncio.run(main())


@pytest.mark.parametrize("model,effort", [("gpt-missing", "high"), ("gpt-selected", "low")])
def test_codex_rejects_unknown_model_and_effort_before_saving(tmp_path, monkeypatch, model, effort):
    async def discover(cfg, env):
        return models()

    monkeypatch.setattr("lynch_reader.chat.service.discover_models", discover)
    svc = codex_service(tmp_path)
    with pytest.raises(Invalid):
        asyncio.run(svc.create("research", None, "hi", model, effort, agent="codex"))
    assert svc._index.all() == []


def test_codex_is_not_a_discover_agent(tmp_path):
    svc = codex_service(tmp_path)
    with pytest.raises(Invalid, match="cannot run"):
        asyncio.run(svc.create("discover", None, "hi", "gpt-selected", "high", agent="codex"))


def test_codex_index_roundtrip_and_bad_thread_type(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(json.dumps([
        {k: v for k, v in conv(id="old").__dict__.items() if k != "codex_thread"},
        {**conv(id="valid").__dict__, "agent": "codex", "codex_thread": "thread-original"},
        {**conv(id="bad").__dict__, "agent": "codex", "codex_thread": 123},
    ]))
    index = ChatIndex(path)
    assert index.get("old").codex_thread is None
    assert index.get("valid").codex_thread == "thread-original"
    assert index.get("bad") is None
    index.update("valid", title="更新")
    assert ChatIndex(path).get("valid").codex_thread == "thread-original"


async def start(svc, text="hello", **kwargs):
    return await svc.create("ticker", "AAA.US", text, None, None, agent="codex", **kwargs)


async def idle(svc, conv_id):
    await until(lambda: state_of(svc, conv_id) == "idle")


def texts(svc, conv_id):
    return [(item["kind"], item.get("text")) for item in svc.snapshot(conv_id)["items"]]


def test_codex_route_defaults_catalog_and_history_survives_server_restart(tmp_path):
    async def main():
        svc = codex_service(tmp_path)
        chat = await start(svc, "first")
        await idle(svc, chat.id)
        await until(lambda: svc.list()["chats"][0]["title"] == "原生短标题")
        row = svc.list()["chats"][0]
        await svc.shutdown()
        again = codex_service(tmp_path)
        assert texts(again, chat.id) == [("user", "first"), ("text", "echo: first")]
        await again.send(chat.id, "second")
        await idle(again, chat.id)
        assert texts(again, chat.id) == [("user", "first"), ("text", "echo: first"),
                                       ("user", "second"), ("text", "echo: second")]
        assert again.list()["chats"][0]["codex_thread"] == row["codex_thread"]
        await again.shutdown()
        return row

    row = asyncio.run(main())
    assert (row["agent_label"], row["model"], row["effort"], row["model_label"], row["title"]) == (
        "Codex", "gpt-test", "low", "GPT Test", "原生短标题")
    assert row["codex_thread"]


def test_codex_http_create_uses_selected_models_default_effort(tmp_path):
    svc = codex_service(tmp_path)
    with TestClient(Starlette(routes=chat_routes(svc, tmp_path / "frameworks"), lifespan=chat_lifespan(svc))) as client:
        response = client.post("/api/chats", json={"kind": "research", "text": "hi", "agent": "codex",
                                                   "model": "gpt-other"})
        assert response.status_code == 200, response.text
        row = client.get("/api/chats").json()["chats"][0]
        assert (row["model"], row["effort"]) == ("gpt-other", "medium")


def test_codex_deltas_context_and_stop_use_existing_events(tmp_path):
    async def main():
        svc = codex_service(tmp_path)
        svc.usage = {"five_hour": 0.5}
        chat = await start(svc)
        await idle(svc, chat.id)
        q = svc.subscribe(chat.id)
        await svc.send(chat.id, "second")
        chunks = [await next_event(q, "delta") for _ in range(2)]
        await idle(svc, chat.id)
        assert "".join(c["text"] for c in chunks) == "echo: second"
        snap = svc.snapshot(chat.id)
        assert snap["usage"] is None
        assert snap["context"]["tokens"] > 0 and snap["context"]["window"] > 0
        await svc.send(chat.id, "wait")
        await svc.send(chat.id, "another")
        assert any(row["text"] == "another" for row in svc.snapshot(chat.id)["queued"])
        await svc.interrupt(chat.id)
        await idle(svc, chat.id)
        assert texts(svc, chat.id)[-1] == ("notice", "已停止")
        for row in svc.snapshot(chat.id)["queued"]:
            svc.dismiss_queued(chat.id, row["id"])
        await svc.send(chat.id, "/compact")
        await idle(svc, chat.id)
        assert texts(svc, chat.id)[-1] == ("compact", None)
        with pytest.raises(Invalid):
            await svc.edit(chat.id, "x", "y")
        await svc.shutdown()
    asyncio.run(main())


@pytest.mark.parametrize("allow", [True, False])
def test_codex_skill_approval_uses_phone_card_and_cannot_be_answered_twice(tmp_path, allow):
    async def main():
        svc = codex_service(tmp_path)
        skill = tmp_path / "frameworks/.claude/skills/sediment-framework/probe.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("before\n")
        chat = await start(svc)
        await idle(svc, chat.id)
        q = svc.subscribe(chat.id)
        await svc.send(chat.id, "skill")
        card = await next_event(q, "approval")
        assert card["path"] == "frameworks/.claude/skills/sediment-framework/probe.md"
        assert "+probe" in card["diff"]
        await svc.answer_approval(chat.id, card["request_id"], allow)
        await idle(svc, chat.id)
        assert svc.snapshot(chat.id)["approvals"] == []
        with pytest.raises(Conflict):
            await svc.answer_approval(chat.id, card["request_id"], allow)
        await svc.shutdown()
    asyncio.run(main())


def test_codex_stop_clears_pending_phone_approval(tmp_path):
    async def main():
        svc = codex_service(tmp_path)
        chat = await start(svc)
        await idle(svc, chat.id)
        q = svc.subscribe(chat.id)
        await svc.send(chat.id, "skill")
        card = await next_event(q, "approval")
        await svc.interrupt(chat.id)
        done = await next_event(q, "approval_done")
        await idle(svc, chat.id)
        assert done["request_id"] == card["request_id"]
        assert svc.snapshot(chat.id)["approvals"] == []
        assert texts(svc, chat.id)[-1] == ("notice", "已停止")
        with pytest.raises(Conflict):
            await svc.answer_approval(chat.id, card["request_id"], True)
        await svc.shutdown()
    asyncio.run(main())


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_only_codex_native_patch_can_supply_the_phone_diff(tmp_path, agent):
    svc = codex_service(tmp_path)
    svc._index.put(conv(agent=agent))
    skill = tmp_path / "frameworks/.claude/skills/sediment-framework/probe.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("before\n")
    approval = svc._skill_approval("c1", "r1", {"tool_name": "Write", "tool_use_id": "p1", "input": {
        "file_path": str(skill), "content": "", "_codex_diff": "+native-new-line\n"}})
    assert approval is not None
    if agent == "codex":
        assert approval.diff == "+native-new-line\n"
    else:
        assert "-before" in approval.diff and "native-new-line" not in approval.diff


def test_codex_idle_process_is_evicted_reaped_and_archived(tmp_path):
    clock = {"t": 0.0}

    async def main():
        svc = codex_service(tmp_path, monotonic=lambda: clock["t"], max_processes=1, idle_timeout_s=100)
        first = await start(svc, "first")
        await idle(svc, first.id)
        link = svc._live[first.id].proc
        second = await start(svc, "second")
        await idle(svc, second.id)
        assert state_of(svc, first.id) == "dormant" and not link.alive
        clock["t"] = 100
        await svc.reap_idle()
        assert state_of(svc, second.id) == "dormant"
        await svc.send(first.id, "wait")
        link = svc._live[first.id].proc
        await svc.archive(first.id)
        assert not link.alive and all(row["id"] != first.id for row in svc.list()["chats"])
        await svc.shutdown()
    asyncio.run(main())


def test_codex_missing_thread_fails_without_replacing_it_or_replaying_history(tmp_path):
    async def main():
        svc = codex_service(tmp_path)
        chat = await start(svc, "original")
        await idle(svc, chat.id)
        thread = svc.list()["chats"][0]["codex_thread"]
        before = texts(svc, chat.id)
        await svc.shutdown()
        (tmp_path / "codex-sessions" / f"{thread}.json").unlink()
        again = codex_service(tmp_path)
        with pytest.raises(Conflict, match="thread not found"):
            await again.send(chat.id, "followup")
        assert texts(again, chat.id) == before
        assert again.list()["chats"][0]["codex_thread"] == thread
        assert state_of(again, chat.id) == "dormant"
        await again.shutdown()
    asyncio.run(main())


def test_codex_crash_and_turn_error_leave_the_chat_usable(tmp_path):
    async def main():
        svc = codex_service(tmp_path)
        chat = await start(svc)
        await idle(svc, chat.id)
        await svc.shutdown()
        broken = codex_service(tmp_path, env={"FAKE_CODEX_CRASH": "1"})
        q = broken.subscribe(chat.id)
        await broken.send(chat.id, "crash")
        await until(lambda: state_of(broken, chat.id) == "dormant")
        notice = await next_event(q, "item", lambda item: item["kind"] == "notice")
        assert "Codex" in notice["text"] and "boom" in notice["text"]
        await broken.shutdown()
        again = codex_service(tmp_path)
        await again.send(chat.id, "error")
        await idle(again, chat.id)
        assert "generation failed" in texts(again, chat.id)[-1][1]
        await again.send(chat.id, "recovered")
        await idle(again, chat.id)
        assert texts(again, chat.id)[-1] == ("text", "echo: recovered")
        await again.shutdown()
    asyncio.run(main())


def test_codex_watch_card_is_available_live_and_can_be_saved(tmp_path):
    card = {"ticker": "AAA.US", "recheck": "查看公告", "waits": [{"what": "公告", "date": "2026-11-10"}]}
    block = "```lynch-watch\n" + json.dumps(card) + "\n```"

    async def main():
        svc = codex_service(tmp_path)
        chat = await start(svc, block)
        await idle(svc, chat.id)
        item = next(item for item in svc.snapshot(chat.id)["items"] if item.get("watch"))
        svc.save_watch(chat.id, item["key"])
        assert svc.watch_list()[0]["recheck"] == "查看公告"
        await svc.shutdown()
    asyncio.run(main())
