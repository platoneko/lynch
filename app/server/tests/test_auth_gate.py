from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lynch_reader.auth.store import DeviceStore
from lynch_reader.web import create_app
from tests.chat_helpers import make_service
from tests.conftest import paired_devices

INVALID = {"error": "配对码无效或已过期"}
UNPAIRED = {"error": "未配对或凭证已吊销"}
HTML = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


@pytest.fixture
def app_parts(repo, tmp_path, quotes):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>lynch</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("1", encoding="utf-8")
    svc, _ = make_service(tmp_path)
    devices, cookies = paired_devices(tmp_path)
    return create_app(repo, dist, quotes, devices, chat=svc), devices, cookies


def anon(app) -> TestClient:
    return TestClient(app, follow_redirects=False)


def test_unpaired_page_load_goes_to_pairing(app_parts):
    app, _, _ = app_parts
    for path in ("/", "/index.html", "/?x=1"):
        r = anon(app).get(path, headers=HTML)
        assert r.status_code == 302 and r.headers["location"] == "/pair", path


def test_unpaired_api_asset_and_stream_are_401(app_parts):
    app, _, cookies = app_parts
    with TestClient(app, cookies=cookies) as paired:
        conv_id = paired.post("/api/chats", json={"kind": "research", "text": "hi"}).json()["id"]
        c = anon(app)
        for path in ("/api/tickers", "/api/frameworks/AAA.US", "/assets/app.js"):
            r = c.get(path)
            assert r.status_code == 401 and r.json() == UNPAIRED, path
        # What EventSource sends for the chat stream.
        r = c.get(f"/api/chats/{conv_id}/events", headers={"Accept": "text/event-stream"})
        assert r.status_code == 401 and r.json() == UNPAIRED
        r = c.post("/api/chats", json={"kind": "research", "text": "hi"})
        assert r.status_code == 401 and r.json() == UNPAIRED
        assert [x["id"] for x in paired.get("/api/chats").json()["chats"]] == [conv_id]


def test_paired_device_gets_through(app_parts):
    app, _, cookies = app_parts
    c = TestClient(app, cookies=cookies)
    assert c.get("/api/tickers").status_code == 200
    assert c.get("/", headers=HTML).status_code == 200


def assert_device_cookie(set_cookie: str, token: str | None = None) -> None:
    parts = [p.strip() for p in set_cookie.split(";")]
    name, _, value = parts[0].partition("=")
    assert name == "lynch_device" and value and (token is None or value == token)
    attrs = {p.lower() for p in parts[1:]}
    assert {"httponly", "samesite=strict", "path=/", "max-age=315360000"} <= attrs
    assert "secure" not in attrs


def test_paired_page_load_renews_the_device_cookie(app_parts):
    # Chromium and Android WebView cap Max-Age at 400 days; a device in use gets its cookie back each load.
    app, _, cookies = app_parts
    c = TestClient(app, cookies=cookies)
    r = c.get("/", headers=HTML)
    assert r.status_code == 200
    assert_device_cookie(r.headers["set-cookie"], cookies["lynch_device"])


def test_paired_api_call_does_not_renew_the_cookie(app_parts):
    app, _, cookies = app_parts
    r = TestClient(app, cookies=cookies).get("/api/tickers")
    assert r.status_code == 200 and "set-cookie" not in r.headers


def test_unpaired_page_load_gets_no_device_cookie(app_parts):
    app, _, _ = app_parts
    r = anon(app).get("/", headers=HTML)
    assert r.status_code == 302 and "lynch_device" not in r.headers.get("set-cookie", "")


def test_revoked_device_is_refused_at_once(app_parts):
    app, devices, cookies = app_parts
    c = TestClient(app, cookies=cookies, follow_redirects=False)
    assert c.get("/api/tickers").status_code == 200
    devices.revoke(devices.devices()[0].id)
    assert c.get("/api/tickers").status_code == 401
    assert c.get("/", headers=HTML).headers["location"] == "/pair"


def test_other_cookies_alongside_do_not_matter(app_parts):
    # The current lynch app's proxy also sends its own gate cookie.
    app, _, cookies = app_parts
    c = TestClient(app, cookies={"lynch_k": "ab" * 32, **cookies})
    assert c.get("/api/tickers").status_code == 200


@pytest.mark.parametrize("value", ["", "garbage", "x" * 5000, "%ff%fe"])
def test_garbage_cookie_is_refused_not_crashed(app_parts, value):
    app, _, _ = app_parts
    c = TestClient(app, cookies={"lynch_device": value}, follow_redirects=False)
    assert c.get("/api/tickers").status_code == 401
    assert c.get("/", headers=HTML).status_code == 302


def test_pairing_page_is_open_and_not_cached(app_parts):
    app, _, _ = app_parts
    r = anon(app).get("/pair?code=ABCD-EFGH", headers=HTML)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert "/api/pair" in r.text and "配对" in r.text


def test_pairing_with_the_code_sets_the_device_cookie(app_parts):
    app, devices, _ = app_parts
    code = devices.create_pairing("mac")
    c = TestClient(app, follow_redirects=False)
    r = c.post("/api/pair", json={"code": code})
    assert r.status_code == 204
    assert_device_cookie(r.headers["set-cookie"])
    assert c.get("/api/tickers").status_code == 200  # the client kept the cookie
    assert [d.name for d in devices.devices()] == ["test", "mac"]


def test_every_pairing_failure_reads_the_same(app_parts):
    app, devices, _ = app_parts
    c = anon(app)

    assert c.post("/api/pair", json={"code": "ABCD-EFGH"}).json() == INVALID  # no pending code

    code = devices.create_pairing("x")
    wrong = "ZZZZ-ZZZZ" if code != "ZZZZ-ZZZZ" else "YYYY-YYYY"
    r = c.post("/api/pair", json={"code": wrong})
    assert r.status_code == 400 and r.json() == INVALID  # wrong

    assert c.post("/api/pair", json={"code": code}).status_code == 204
    assert c.post("/api/pair", json={"code": code}).json() == INVALID  # used

    code = devices.create_pairing("x")
    for _ in range(5):
        c.post("/api/pair", json={"code": wrong})
    assert c.post("/api/pair", json={"code": code}).json() == INVALID  # voided

    for body in ({}, {"code": 123}, ["x"]):
        r = c.post("/api/pair", json=body)
        assert r.status_code == 400 and r.json() == INVALID, body


def test_expired_code_reads_the_same(repo, tmp_path, quotes, clock):
    devices = DeviceStore(tmp_path / "auth" / "devices.json", clock=clock)
    app = create_app(repo, tmp_path / "no-dist", quotes, devices)
    code = devices.create_pairing("x")
    clock.now += 600
    r = anon(app).post("/api/pair", json={"code": code})
    assert r.status_code == 400 and r.json() == INVALID


def test_pairing_post_must_be_json(app_parts):
    app, devices, _ = app_parts
    code = devices.create_pairing("x")
    r = anon(app).post("/api/pair", content=f'{{"code": "{code}"}}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert anon(app).post("/api/pair", json={"code": code}).status_code == 204  # the code was not spent


TOO_LARGE = {"error": "请求体过大"}


def test_pairing_post_over_one_kib_is_refused(app_parts):
    app, devices, _ = app_parts
    code = devices.create_pairing("x")
    c = anon(app)
    big = {"code": code, "pad": "x" * 2048}
    r = c.post("/api/pair", json=big)
    assert r.status_code == 413 and r.json() == TOO_LARGE
    # Chunked, so no Content-Length to go by.
    body = json.dumps(big).encode()
    r = c.post("/api/pair", content=iter([body[:1000], body[1000:]]), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == TOO_LARGE
    assert c.post("/api/pair", json={"code": code}).status_code == 204  # the code was not spent


def test_only_the_two_pairing_routes_are_open(app_parts):
    app, _, _ = app_parts
    c = anon(app)
    assert c.get("/api/pair").status_code == 401
    assert c.post("/pair", json={}).status_code == 401
    assert c.get("/pair/", headers=HTML).status_code == 302


def test_corrupt_store_refuses_everything(app_parts):
    app, devices, cookies = app_parts
    devices.path.write_text("not json")
    c = TestClient(app, cookies=cookies, follow_redirects=False)
    assert c.get("/api/tickers").status_code == 401
    assert c.get("/", headers=HTML).status_code == 302
    r = anon(app).post("/api/pair", json={"code": "ABCD-EFGH"})
    assert r.status_code == 500 and "error" in r.json()
    assert devices.path.read_text() == "not json"


def test_websocket_without_a_device_is_closed(app_parts):
    app, _, _ = app_parts
    with pytest.raises(WebSocketDisconnect):
        with anon(app).websocket_connect("/ws"):
            pass
