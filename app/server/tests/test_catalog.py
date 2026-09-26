from __future__ import annotations

import json
import logging

import pytest

from lynch_reader.catalog import catalog, framework_path
from tests.conftest import AS_OF


def rows_by_ticker(fw, bands):
    return {r["ticker"]: r for r in catalog(fw, bands)["tickers"]}


def test_labels_come_from_eval_lib(repo):
    out = catalog(*repo)
    assert [z["id"] for z in out["zones"]] == ["deep_discount", "conservative", "neutral", "optimistic", "stretched"]
    assert out["zones"][1]["label"] == "保守"
    assert {"id": "fast_grower", "label": "快速增长"} in out["lynch_classes"]


def test_pool_order_and_template_excluded(repo):
    assert [r["ticker"] for r in catalog(*repo)["tickers"]] == ["AAA.US", "BBB.US", "0001.HK"]


def test_parsed_row(repo):
    row = rows_by_ticker(*repo)["AAA.US"]
    assert row == {
        "ticker": "AAA.US", "name": "Alpha Corp", "market": "US",
        "lynch_primary": "fast_grower", "lynch_secondary": "cyclical", "currency": "USD",
        "L": 10.0, "M": 20.0, "H": 40.0, "blocked": None, "candidate_label": None,
        "band": {"price": 25.0, "as_of": AS_OF, "zone": "neutral", "option_zone": None,
                 "candidate_zone": "optimistic"},
        "error": None,
    }


def test_blocked_row_keeps_price_without_zone(repo):
    row = rows_by_ticker(*repo)["BBB.US"]
    assert row["blocked"] == "test reason; unblock when X"
    assert row["band"]["price"] == 5.0
    assert row["band"]["zone"] is None


def test_parse_failure_is_one_row_not_whole_list(repo):
    row = rows_by_ticker(*repo)["0001.HK"]
    assert row["error"]
    assert row["name"] is None and row["band"] is None and row["market"] is None


def test_missing_band_is_null(repo):
    fw, bands = repo
    (bands / "AAA.US.json").unlink()
    assert rows_by_ticker(fw, bands)["AAA.US"]["band"] is None


def test_malformed_band_is_null(repo):
    fw, bands = repo
    (bands / "AAA.US.json").write_text("{not json", encoding="utf-8")
    assert rows_by_ticker(fw, bands)["AAA.US"]["band"] is None


@pytest.mark.parametrize("content", [
    b"[]",
    b"42",
    b"\xff\xfe\x00",
    json.dumps({"price": True, "as_of": AS_OF, "zone": "neutral"}).encode(),
    json.dumps({"price": float("nan"), "as_of": AS_OF, "zone": "neutral"}).encode(),
    json.dumps({"price": float("inf"), "as_of": AS_OF, "zone": "neutral"}).encode(),
], ids=["list", "number", "non-utf8", "bool-price", "nan-price", "inf-price"])
def test_odd_band_is_one_null_row_with_warning(repo, caplog, content):
    fw, bands = repo
    (bands / "AAA.US.json").write_bytes(content)
    with caplog.at_level(logging.WARNING, logger="lynch_reader"):
        rows = rows_by_ticker(fw, bands)
    assert rows["AAA.US"]["band"] is None
    assert rows["BBB.US"]["band"]["price"] == 5.0
    assert any(r.levelno == logging.WARNING and "AAA.US" in r.getMessage() for r in caplog.records)


def test_band_without_price_is_null(repo):
    fw, bands = repo
    (bands / "AAA.US.json").write_text(json.dumps({"zone": "neutral", "as_of": AS_OF}), encoding="utf-8")
    assert rows_by_ticker(fw, bands)["AAA.US"]["band"] is None


def test_framework_path_only_resolves_pool_members(repo):
    fw, _ = repo
    assert framework_path("AAA.US", fw) == fw / "AAA.US.md"
    assert framework_path("_TEMPLATE", fw) is None
    assert framework_path("../AGENTS", fw) is None
    assert framework_path("NOPE.US", fw) is None
