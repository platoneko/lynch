from __future__ import annotations

from starlette.testclient import TestClient

from lynch_reader.archive import FrameworkArchive
from lynch_reader.catalog import QUOTE_REFRESH_MAX_AGE_S, QuoteCache
from lynch_reader.web import create_app
from tests.conftest import FakeFetch, paired_devices


def client(repo, tmp_path, quotes, with_dist=True):
    dist = tmp_path / "dist"
    if with_dist:
        dist.mkdir()
        (dist / "index.html").write_text("<!doctype html><title>lynch</title>", encoding="utf-8")
    devices, cookies = paired_devices(tmp_path)
    archive = FrameworkArchive(tmp_path / "archive" / "frameworks.json")
    return TestClient(create_app(repo, dist, quotes, devices, archive), cookies=cookies)


def test_tickers_json(repo, tmp_path, quotes):
    r = client(repo, tmp_path, quotes).get("/api/tickers")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"zones", "lynch_classes", "quote_error", "axes", "tickers"}
    assert body["quote_error"] is None
    assert body["tickers"][0]["ticker"] == "AAA.US"
    assert body["tickers"][0]["band"]["price"] == 25.0


def test_refresh_query_uses_the_refresh_max_age(repo, tmp_path, fetch, clock, quotes):
    c = client(repo, tmp_path, quotes)
    c.get("/api/tickers")
    clock.now += QUOTE_REFRESH_MAX_AGE_S
    c.get("/api/tickers")
    assert len(fetch.calls) == 1
    c.get("/api/tickers?refresh=1")
    assert len(fetch.calls) == 2


def test_non_finite_price_nulls_only_its_row(repo, tmp_path, fetch, quotes):
    # A NaN quote must not reach the JSON response, which cannot carry it.
    fetch.prices["AAA.US"] = float("nan")
    r = client(repo, tmp_path, quotes).get("/api/tickers")
    assert r.status_code == 200
    rows = {t["ticker"]: t for t in r.json()["tickers"]}
    assert rows["AAA.US"]["band"] is None and rows["AAA.US"]["quote_error"]
    assert rows["BBB.US"]["band"]["price"] == 5.0


def test_quote_failure_is_200_with_top_level_error(repo, tmp_path, clock):
    quotes = QuoteCache(
        fetch=FakeFetch({}, error=RuntimeError("longbridge CLI not found on PATH")), monotonic=clock, wall=clock
    )
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
