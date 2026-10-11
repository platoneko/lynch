from __future__ import annotations

from starlette.testclient import TestClient

from lynch_reader.anomalies import Anomalies, AnomalyStore, BaselineCache, Hit
from lynch_reader.archive import FrameworkArchive
from lynch_reader.catalog import QuoteCache
from lynch_reader.earnings import CalendarCache, Earnings, IgnoreStore
from lynch_reader.web import create_app
from eval.lib.earnings_lb import CalendarEvent, CalendarFetch  # after lynch_reader, which puts the repo root on sys.path
from tests.conftest import NOW, FakeFetch, paired_devices
from tests.test_anomalies import FakeBars, us_bars


def fake_calendar(tickers, start, end):
    events = {"AAA.US": [CalendarEvent("2026-09-21", "before", "2026 财年第 3 季度")]}
    return CalendarFetch({t: events.get(t, []) for t in tickers}, [], None)


def client(repo, tmp_path, quotes, with_dist=True, calendar=fake_calendar, bars=None, poller=None):
    dist = tmp_path / "dist"
    if with_dist:
        dist.mkdir()
        (dist / "index.html").write_text("<!doctype html><title>lynch</title>", encoding="utf-8")
    devices, cookies = paired_devices(tmp_path)
    archive = FrameworkArchive(tmp_path / "archive" / "frameworks.json")
    wall = lambda: NOW  # noqa: E731 — Mon 2026-09-21 10:13 New York: AAA's before-open release is out
    cal = CalendarCache(tmp_path / "earnings" / "calendar.json", fetch=calendar, wall=wall)
    earnings = Earnings(cal, IgnoreStore(tmp_path / "earnings" / "ignored.json", wall=wall), wall=wall)
    baselines = BaselineCache(tmp_path / "anomalies" / "baseline.json", fetch=bars or FakeBars(us_bars()),
                              wall=wall, sleep=lambda s: None)
    anomalies = Anomalies(AnomalyStore(tmp_path / "anomalies" / "events.json", wall=wall), baselines, cal, wall=wall)
    app = create_app(repo, dist, quotes, devices, archive, earnings=earnings, anomalies=anomalies, poller=poller)
    c = TestClient(app, cookies=cookies)
    c.anomalies = anomalies  # tests reach the store and the upkeep through the client
    return c


def test_a_new_batch_is_judged_for_the_active_pool(repo, tmp_path, fetch, quotes):
    c = client(repo, tmp_path, quotes)
    c.anomalies.upkeep(["AAA.US", "BBB.US"])  # the poller's daily baseline fetch
    # BBB at 5.0 on a 4.0 base close is +25%; AAA (on the earnings list in fake_calendar) has no base close.
    fetch.prev["BBB.US"] = 4.0
    c.get("/api/tickers")  # the first read fetches; the listener judges the batch
    items = c.get("/api/anomalies").json()["items"]
    assert [(i["ticker"], i["ref"], i["reason"]) for i in items] == [("BBB.US", "4.0", "move")]
    c.post("/api/frameworks/BBB.US/archive", json={})
    assert c.get("/api/anomalies").json()["items"] == []  # archived: not listed


def test_anomaly_list_never_fetches_bars(repo, tmp_path, quotes):
    bars = FakeBars(us_bars())
    c = client(repo, tmp_path, quotes, bars=bars)
    body = c.get("/api/anomalies").json()
    assert body == {"error": None, "items": []} and bars.calls == []


def test_anomaly_ignore_and_unignore(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    c.anomalies.store.record([("AAA.US", "20.0", Hit(0.25, 25.0, "regular", NOW, "move", None))], lambda t: False)
    assert c.post("/api/anomalies/ignore", json={"ticker": "AAA.US", "ref": "20.0"}).status_code == 204
    assert c.get("/api/anomalies").json()["items"][0]["ignored"] is True
    assert c.post("/api/anomalies/unignore", json={"ticker": "AAA.US", "ref": "20.0"}).status_code == 204
    assert c.get("/api/anomalies").json()["items"][0]["ignored"] is False


def test_anomaly_ignore_refuses_bad_input(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    for body in ({"ticker": "NOPE.US", "ref": "20.0"}, {"ticker": "AAA.US", "ref": "1.0"},
                 {"ticker": "AAA.US"}, {"ticker": 1, "ref": "20.0"}):
        r = c.post("/api/anomalies/ignore", json=body)
        assert r.status_code == 400 and r.json()["error"], body
    r = c.post("/api/anomalies/ignore", content="x", headers={"content-type": "text/plain"})
    assert r.status_code == 415


def test_earnings_items_carry_their_anomaly(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    c.anomalies.store.record([("AAA.US", "20.0", Hit(0.25, 25.0, "post", NOW, "move", None))], lambda t: True)
    [item] = c.get("/api/earnings").json()["items"]
    assert item["anomaly"]["session"] == "post"
    assert c.get("/api/anomalies").json()["items"] == []


def test_lifespan_starts_and_stops_the_poller(repo, tmp_path, quotes):
    class FakePoller:
        started = stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True
    p = FakePoller()
    with client(repo, tmp_path, quotes, poller=p) as c:
        assert p.started and not p.stopped
        c.get("/api/tickers")
    assert p.stopped


def test_earnings_lists_pool_tickers(repo, tmp_path, quotes):
    body = client(repo, tmp_path, quotes).get("/api/earnings").json()
    assert body["error"] is None and body["fetched_day"] == "2026-09-21"
    assert [(i["ticker"], i["status"]) for i in body["items"]] == [("AAA.US", "released")]


def test_earnings_skip_archived_tickers(repo, tmp_path, quotes):
    seen: list[list[str]] = []

    def calendar(tickers, start, end):
        seen.append(list(tickers))
        return fake_calendar(tickers, start, end)
    c = client(repo, tmp_path, quotes, calendar=calendar)
    c.post("/api/frameworks/AAA.US/archive", json={})
    assert c.get("/api/earnings").json()["items"] == []
    assert "AAA.US" not in seen[0]


def test_ignore_and_unignore(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    assert c.post("/api/earnings/ignore", json={"ticker": "AAA.US", "date": "2026-09-21"}).status_code == 204
    assert c.get("/api/earnings").json()["items"][0]["ignored"] is True
    assert c.post("/api/earnings/unignore", json={"ticker": "AAA.US", "date": "2026-09-21"}).status_code == 204
    assert c.get("/api/earnings").json()["items"][0]["ignored"] is False


def test_ignore_refuses_bad_input(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    for body in ({"ticker": "NOPE.US", "date": "2026-09-21"}, {"ticker": "AAA.US", "date": "09/21"},
                 {"ticker": "AAA.US"}, {"ticker": 1, "date": "2026-09-21"}):
        r = c.post("/api/earnings/ignore", json=body)
        assert r.status_code == 400 and r.json()["error"], body
    r = c.post("/api/earnings/ignore", content="ticker=AAA.US", headers={"content-type": "text/plain"})
    assert r.status_code == 415
    assert not (tmp_path / "earnings" / "ignored.json").exists()


def test_tickers_json(repo, tmp_path, quotes):
    r = client(repo, tmp_path, quotes).get("/api/tickers")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"zones", "lynch_classes", "quote_error", "axes", "tickers"}
    assert body["quote_error"] is None
    assert body["tickers"][0]["ticker"] == "AAA.US"
    assert body["tickers"][0]["band"]["price"] == 25.0


def test_non_finite_price_nulls_only_its_row(repo, tmp_path, fetch, quotes):
    # A NaN quote must not reach the JSON response, which cannot carry it.
    fetch.prices["AAA.US"] = float("nan")
    r = client(repo, tmp_path, quotes).get("/api/tickers")
    assert r.status_code == 200
    rows = {t["ticker"]: t for t in r.json()["tickers"]}
    assert rows["AAA.US"]["band"] is None and rows["AAA.US"]["quote_error"]
    assert rows["BBB.US"]["band"]["price"] == 5.0


def test_quote_failure_is_200_with_top_level_error(repo, tmp_path, clock):
    quotes = QuoteCache(fetch=FakeFetch({}, error=RuntimeError("longbridge CLI not found on PATH")), wall=clock)
    r = client(repo, tmp_path, quotes).get("/api/tickers")
    assert r.status_code == 200
    body = r.json()
    assert body["quote_error"]
    assert all(t["band"] is None for t in body["tickers"])


def test_framework_markdown(repo, tmp_path, quotes):
    r = client(repo, tmp_path, quotes).get("/api/frameworks/AAA.US")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.text == (repo / "AAA.US.md").read_text(encoding="utf-8")


def test_tickers_answer_304_while_the_list_is_unchanged(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    etag = c.get("/api/tickers").headers["etag"]
    held = c.get("/api/tickers", headers={"If-None-Match": etag})
    assert held.status_code == 304 and held.content == b""
    c.post("/api/frameworks/AAA.US/archive", json={})
    changed = c.get("/api/tickers", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag


def test_documents_answer_304_until_the_file_changes(repo, tmp_path, quotes):
    _axis = repo / "axes"
    _axis.mkdir()
    (_axis / "al.md").write_text("# _AXIS.al — AL 价格情景轴\n> 轴类型：`shared_scenario_axis` | 挂载票：AAA.US\n",
                                 encoding="utf-8")
    c = client(repo, tmp_path, quotes)
    for path, file in (("/api/frameworks/AAA.US", repo / "AAA.US.md"), ("/api/axes/al", _axis / "al.md")):
        etag = c.get(path).headers["etag"]
        assert c.get(path, headers={"If-None-Match": etag}).status_code == 304
        file.write_text(file.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
        r = c.get(path, headers={"If-None-Match": etag})
        assert r.status_code == 200 and r.text.endswith("edited\n")


def test_framework_outside_pool_is_404(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    for path in ("/api/frameworks/_TEMPLATE", "/api/frameworks/NOPE.US",
                 "/api/frameworks/..%2F..%2FAGENTS", "/api/frameworks/../../AGENTS.md"):
        assert c.get(path).status_code == 404, path


def test_static_index(repo, tmp_path, quotes):
    r = client(repo, tmp_path, quotes).get("/")
    assert r.status_code == 200
    assert "<title>lynch</title>" in r.text


def test_index_is_revalidated(repo, tmp_path, quotes):
    # The phone's proxy origin is stable across launches, so a cached index.html
    # would keep pointing at the previous build's hashed assets.
    c = client(repo, tmp_path, quotes)
    for path in ("/", "/index.html"):
        assert c.get(path).headers.get("cache-control") == "no-cache", path


def test_api_works_without_built_web(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes, with_dist=False)
    assert c.get("/api/tickers").status_code == 200
    assert c.get("/").status_code == 404


def test_axis_markdown(repo, tmp_path, quotes):
    (repo / "axes").mkdir()
    (repo / "axes" / "al.md").write_text("# _AXIS.al — 铝情景轴\n", encoding="utf-8")
    r = client(repo, tmp_path, quotes).get("/api/axes/al")
    assert r.status_code == 200
    assert r.text.startswith("# _AXIS.al")
    assert r.headers["content-type"].startswith("text/markdown")


def test_unknown_axis_is_404(repo, tmp_path, quotes):
    (repo / "axes").mkdir()
    c = client(repo, tmp_path, quotes)
    assert c.get("/api/axes/nope").status_code == 404
    assert c.get("/api/axes/..%2FAAA.US").status_code == 404


def archived_flags(c) -> dict[str, bool]:
    return {t["ticker"]: t["archived"] for t in c.get("/api/tickers").json()["tickers"]}


def test_archive_marks_the_row_and_keeps_its_document(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    assert archived_flags(c) == {"AAA.US": False, "BBB.US": False, "0001.HK": False}
    assert c.post("/api/frameworks/AAA.US/archive", json={}).status_code == 204
    assert archived_flags(c) == {"AAA.US": True, "BBB.US": False, "0001.HK": False}
    assert c.get("/api/frameworks/AAA.US").status_code == 200
    # Still priced: the list shows archived rows when asked for.
    row = next(t for t in c.get("/api/tickers").json()["tickers"] if t["ticker"] == "AAA.US")
    assert row["band"]["price"] == 25.0


def test_archive_survives_a_restart_and_restore_undoes_it(repo, tmp_path, quotes):
    client(repo, tmp_path, quotes, with_dist=False).post("/api/frameworks/AAA.US/archive", json={})
    c = client(repo, tmp_path, quotes, with_dist=False)  # a fresh store on the same file
    assert archived_flags(c)["AAA.US"] is True
    assert c.post("/api/frameworks/AAA.US/restore", json={}).status_code == 204
    assert archived_flags(c)["AAA.US"] is False


def test_archive_twice_and_restore_unarchived_are_no_ops(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    for _ in range(2):
        assert c.post("/api/frameworks/AAA.US/archive", json={}).status_code == 204
    assert c.post("/api/frameworks/BBB.US/restore", json={}).status_code == 204
    assert archived_flags(c) == {"AAA.US": True, "BBB.US": False, "0001.HK": False}


def test_archive_only_takes_pool_members(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    for path in ("/api/frameworks/NOPE.US/archive", "/api/frameworks/_TEMPLATE/archive",
                 "/api/frameworks/NOPE.US/restore"):
        assert c.post(path, json={}).status_code == 400, path
    assert not (tmp_path / "archive" / "frameworks.json").exists()


def test_archive_refuses_a_non_json_post(repo, tmp_path, quotes):
    # CSRF guard: a cross-site form post must not archive anything.
    c = client(repo, tmp_path, quotes)
    assert c.post("/api/frameworks/AAA.US/archive", content=b"", headers={"Content-Type": "text/plain"}).status_code == 415
    assert archived_flags(c)["AAA.US"] is False
