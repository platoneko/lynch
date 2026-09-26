from __future__ import annotations

from starlette.testclient import TestClient

from lynch_reader.web import create_app


def client(repo, tmp_path, with_dist=True):
    dist = tmp_path / "dist"
    if with_dist:
        dist.mkdir()
        (dist / "index.html").write_text("<!doctype html><title>lynch</title>", encoding="utf-8")
    fw, bands = repo
    return TestClient(create_app(fw, bands, dist))


def test_tickers_json(repo, tmp_path):
    r = client(repo, tmp_path).get("/api/tickers")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"zones", "lynch_classes", "tickers"}
    assert body["tickers"][0]["ticker"] == "AAA.US"


def test_non_finite_price_nulls_only_its_row(repo, tmp_path):
    # Python's json.loads accepts NaN; serializing it back must not fail the whole list.
    (repo[1] / "AAA.US.json").write_text('{"price": NaN, "as_of": "x", "zone": "neutral"}', encoding="utf-8")
    r = client(repo, tmp_path).get("/api/tickers")
    assert r.status_code == 200
    rows = {t["ticker"]: t for t in r.json()["tickers"]}
    assert rows["AAA.US"]["band"] is None
    assert rows["BBB.US"]["band"]["price"] == 5.0


def test_framework_markdown(repo, tmp_path):
    r = client(repo, tmp_path).get("/api/frameworks/AAA.US")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.text == (repo[0] / "AAA.US.md").read_text(encoding="utf-8")


def test_framework_outside_pool_is_404(repo, tmp_path):
    c = client(repo, tmp_path)
    for path in ("/api/frameworks/_TEMPLATE", "/api/frameworks/NOPE.US",
                 "/api/frameworks/..%2F..%2FAGENTS", "/api/frameworks/../../AGENTS.md"):
        assert c.get(path).status_code == 404, path


def test_static_index(repo, tmp_path):
    r = client(repo, tmp_path).get("/")
    assert r.status_code == 200
    assert "<title>lynch</title>" in r.text


def test_index_is_revalidated(repo, tmp_path):
    # The phone's proxy origin is stable across launches, so a cached index.html
    # would keep pointing at the previous build's hashed assets.
    c = client(repo, tmp_path)
    for path in ("/", "/index.html"):
        assert c.get(path).headers.get("cache-control") == "no-cache", path


def test_api_works_without_built_web(repo, tmp_path):
    c = client(repo, tmp_path, with_dist=False)
    assert c.get("/api/tickers").status_code == 200
    assert c.get("/").status_code == 404
