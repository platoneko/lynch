from __future__ import annotations

import asyncio
import json

from starlette.testclient import TestClient

from lynch_reader.chat.routes import chat_lifespan, event_stream
from lynch_reader.web import create_app
from tests.chat_helpers import make_service, state_of, until
from tests.conftest import paired_devices


def client(repo, tmp_path, quotes):
    svc, _ = make_service(tmp_path)
    devices, cookies = paired_devices(tmp_path)
    return TestClient(create_app(repo, tmp_path / "no-dist", quotes, devices, chat=svc), cookies=cookies), svc


def test_create_list_send_and_errors(repo, tmp_path, quotes):
    c, svc = client(repo, tmp_path, quotes)
    with c:
        assert c.get("/api/chats/options").json()["default_effort"] == "medium"
        bad = c.post("/api/chats", json={"kind": "ticker", "ticker": "ZZZ.US", "text": "hi"})
        assert bad.status_code == 400 and "error" in bad.json()
        r = c.post("/api/chats", json={"kind": "ticker", "ticker": "AAA.US", "text": "hi"})
        conv_id = r.json()["id"]
        assert [x["id"] for x in c.get("/api/chats").json()["chats"]] == [conv_id]
        assert c.post(f"/api/chats/{conv_id}/messages", json={"text": ""}).status_code == 400
        assert c.post("/api/chats/nope/messages", json={"text": "x"}).status_code == 404
        assert c.post(f"/api/chats/{conv_id}/interrupt", json={}).status_code == 204
        assert c.post(f"/api/chats/{conv_id}/edit", json={"message_uuid": "x", "text": "y"}).status_code in (400, 409)
        assert c.get("/api/chats/nope/events").status_code == 404
        assert c.post(f"/api/chats/{conv_id}/title", json={"title": "AAA 估值"}).status_code == 204
        assert c.get("/api/chats").json()["chats"][0]["title"] == "AAA 估值"
        assert c.post(f"/api/chats/{conv_id}/title", json={"title": ""}).status_code == 400
        assert c.post("/api/chats/archive", json={"ticker": "AAA.US"}).json() == {"archived": 1}
        assert c.get("/api/chats").json()["chats"] == []


def test_default_model_and_effort(repo, tmp_path, quotes):
    c, svc = client(repo, tmp_path, quotes)
    with c:
        conv_id = c.post("/api/chats", json={"kind": "research", "text": "hi"}).json()["id"]
        row = c.get("/api/chats").json()["chats"][0]
        assert (row["id"], row["model"], row["effort"]) == (conv_id, "claude-opus-5-5", "medium")


def test_event_stream_starts_with_a_snapshot_then_live_events(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        stop = asyncio.Event()

        async def disconnected():
            return stop.is_set()

        stream = event_stream(svc, conv.id, disconnected)
        first = await anext(stream)
        await svc.send(conv.id, "two")
        frames = [first]
        while "echo: two" not in frames[-1]:
            frames.append(await anext(stream))
        stop.set()
        await stream.aclose()
        await svc.shutdown()
        return frames, svc
    frames, svc = asyncio.run(main())
    event, data = frames[0].split("\n")[:2]
    assert event == "event: snapshot"
    assert [i["text"] for i in json.loads(data[len("data: "):])["items"]] == ["one", "echo: one"]
    assert any(f.startswith("event: delta") for f in frames)
    assert svc._live and all(not live.subscribers for live in svc._live.values())  # unsubscribed on close


def test_a_quiet_stream_sends_a_heartbeat_the_page_can_see(tmp_path, monkeypatch):
    # An SSE comment never reaches the page's listeners, so it could not tell a quiet stream from a dead one.
    monkeypatch.setattr("lynch_reader.chat.routes.HEARTBEAT_S", 0.05)

    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")

        async def connected():
            return False

        stream = event_stream(svc, conv.id, connected)
        frames = [await anext(stream)]  # the snapshot, then maybe the title claude names after the first reply

        async def until_quiet():
            while frames[-1].startswith(("event: snapshot", "event: conv")):
                frames.append(await anext(stream))
        await asyncio.wait_for(until_quiet(), 5)
        await stream.aclose()
        await svc.shutdown()
        return frames[-1]
    assert asyncio.run(main()).startswith("event: ping\n")


def test_app_without_chat_has_no_chat_routes(repo, tmp_path, quotes):
    devices, cookies = paired_devices(tmp_path)
    c = TestClient(create_app(repo, tmp_path / "no-dist", quotes, devices), cookies=cookies)
    assert c.get("/api/chats").status_code == 404


def test_bad_bodies_are_400(repo, tmp_path, quotes):
    c, svc = client(repo, tmp_path, quotes)
    with c:
        not_utf8 = c.post("/api/chats", content=b"\xff\xfe{", headers={"Content-Type": "application/json"})
        assert not_utf8.status_code == 400 and "error" in not_utf8.json()
        wrong_type = c.post("/api/chats", json={"kind": "research", "text": "hi", "model": ["x"]})
        assert wrong_type.status_code == 400 and "error" in wrong_type.json()
        assert c.get("/api/chats").json()["chats"] == []


def test_archive_one_chat(repo, tmp_path, quotes):
    c, svc = client(repo, tmp_path, quotes)
    with c:
        conv_id = c.post("/api/chats", json={"kind": "research", "text": "hi"}).json()["id"]
        assert c.post(f"/api/chats/{conv_id}/archive", json={}).status_code == 204
        assert c.get("/api/chats").json()["chats"] == []
        assert c.post(f"/api/chats/{conv_id}/archive", json={}).status_code == 404


def test_posts_without_json_content_type_are_415(repo, tmp_path, quotes):
    # A cross-site "simple" POST (text/plain) must not create, interrupt or archive a chat.
    c, svc = client(repo, tmp_path, quotes)
    with c:
        forged = c.post("/api/chats", content=json.dumps({"kind": "research", "text": "hi"}),
                        headers={"Content-Type": "text/plain"})
        assert forged.status_code == 415 and "error" in forged.json()
        assert c.get("/api/chats").json()["chats"] == []

        conv_id = c.post("/api/chats", json={"kind": "research", "text": "slow"}).json()["id"]
        for path in (f"/api/chats/{conv_id}/interrupt", f"/api/chats/{conv_id}/archive",
                     f"/api/chats/{conv_id}/messages", "/api/chats/archive"):
            r = c.post(path, content="{}", headers={"Content-Type": "text/plain"})
            assert r.status_code == 415 and "error" in r.json(), path
            assert c.post(path).status_code == 415, path  # no Content-Type at all
        assert [x["id"] for x in c.get("/api/chats").json()["chats"]] == [conv_id]
        assert svc._live[conv_id].proc is not None  # not killed

        ok = c.post(f"/api/chats/{conv_id}/interrupt", json={},
                    headers={"Content-Type": "application/json; charset=utf-8"})
        assert ok.status_code == 204


def test_reaper_survives_a_failed_sweep():
    class Stub:
        def __init__(self):
            self.sweeps = 0
            self.shut = False

        async def reap_idle(self):
            self.sweeps += 1
            if self.sweeps == 1:
                raise RuntimeError("watcher failed")

        async def shutdown(self):
            self.shut = True

    async def main():
        stub = Stub()
        async with chat_lifespan(stub, reap_interval_s=0.01)(None):
            await until(lambda: stub.sweeps >= 3)
        return stub
    stub = asyncio.run(main())
    assert stub.sweeps >= 3 and stub.shut


def test_answer_an_approval(repo, tmp_path, quotes):
    import time
    from lynch_reader.chat.approvals import APPROVABLE_SKILL
    f = tmp_path / APPROVABLE_SKILL / "r.md"
    f.parent.mkdir(parents=True)
    f.write_text("alpha\n")
    c, svc = client(repo, tmp_path, quotes)
    with c:
        conv_id = c.post("/api/chats", json={"kind": "research", "text": f"ask-edit {f}"}).json()["id"]
        deadline = time.monotonic() + 5
        while not svc.snapshot(conv_id)["approvals"] and time.monotonic() < deadline:
            time.sleep(0.02)
        rid = svc.snapshot(conv_id)["approvals"][0]["request_id"]
        url = f"/api/chats/{conv_id}/approvals/{rid}"
        assert c.post(url, json={"allow": "yes"}).status_code == 400
        assert c.post(url, content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
        assert c.post(f"/api/chats/nope/approvals/{rid}", json={"allow": True}).status_code == 404
        assert c.post(url, json={"allow": True}).status_code == 204
        assert c.post(url, json={"allow": True}).status_code == 409
        while state_of(svc, conv_id) != "idle" and time.monotonic() < deadline:
            time.sleep(0.02)
    assert f.read_text() == "beta\n"


def test_discover_create_save_list_and_dismiss(repo, tmp_path, quotes):
    c, svc = client(repo, tmp_path, quotes)
    with c:
        assert c.get("/api/chats/options").json()["discover"]["default_model"] == "grok-4.6"
        bad = c.post("/api/chats", json={"kind": "discover", "text": "x", "model": "grok-4.6", "effort": "high",
                                         "context_window": 123})
        assert bad.status_code == 400
        conv_id = c.post("/api/chats", json={"kind": "discover", "text": "candidates", "model": "grok-4.6",
                                             "effort": "high", "context_window": 500000,
                                             "lookback_days": 3}).json()["id"]
        assert svc.snapshot(conv_id)["conv"]["lookback_days"] == 3
        asyncio.run(until(lambda: state_of(svc, conv_id) == "dormant"
                          and any(i.get("candidates") for i in svc.snapshot(conv_id)["items"])))
        [item] = [i for i in svc.snapshot(conv_id)["items"] if i.get("candidates")]
        r = c.post(f"/api/chats/{conv_id}/candidates", json={"key": item["key"], "tickers": ["AXTI.US"]})
        assert r.json() == {"saved": ["AXTI.US"]}
        assert c.post(f"/api/chats/{conv_id}/candidates", json={"key": "nope", "tickers": ["AXTI.US"]}).status_code == 409
        assert [x["ticker"] for x in c.get("/api/candidates").json()["candidates"]] == ["AXTI.US"]
        assert c.post("/api/candidates/AXTI.US/dismiss", json={}).status_code == 204
        assert c.get("/api/candidates").json()["candidates"][0]["dismissed"] is True
        assert c.post("/api/candidates/NOPE.US/dismiss", json={}).status_code == 400
        assert c.post("/api/candidates/AXTI.US/dismiss", data="x").status_code == 415  # the CSRF guard holds
