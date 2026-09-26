from __future__ import annotations

import logging
import re
import subprocess
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from eval.lib.frameworks import list_framework_paths
from eval.lib.zones import zone_of
from lynch_reader.catalog import (
    FRAMEWORKS_DIR, NO_QUOTE, QUOTE_BACKOFF_S, QUOTE_MAX_AGE_S, QUOTE_REFRESH_MAX_AGE_S, QuoteCache, catalog, framework_path,
)
from tests.conftest import NOW, FakeClock, FakeFetch, framework_md

needs_content = pytest.mark.skipif(not FRAMEWORKS_DIR.is_dir(), reason="content repo not cloned at frameworks/")

AS_OF = datetime.fromtimestamp(NOW, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def rows_by_ticker(fw, quotes):
    return {r["ticker"]: r for r in catalog(fw, quotes)["tickers"]}


def test_labels_come_from_eval_lib(repo, quotes):
    out = catalog(repo, quotes)
    assert [z["id"] for z in out["zones"]] == ["deep_discount", "conservative", "neutral", "optimistic", "stretched"]
    assert out["zones"][1]["label"] == "保守"
    assert {"id": "fast_grower", "label": "快速增长"} in out["lynch_classes"]


def test_pool_order_and_template_excluded(repo, quotes):
    assert [r["ticker"] for r in catalog(repo, quotes)["tickers"]] == ["AAA.US", "BBB.US", "0001.HK"]


def test_parsed_row_is_zoned_on_the_live_price(repo, quotes):
    out = catalog(repo, quotes)
    assert out["quote_error"] is None
    row = {r["ticker"]: r for r in out["tickers"]}["AAA.US"]
    assert row == {
        "ticker": "AAA.US", "name": "Alpha Corp", "market": "US",
        "lynch_primary": "fast_grower", "lynch_secondary": "cyclical", "currency": "USD",
        "L": 10.0, "M": 20.0, "H": 40.0, "option": None, "candidate": None,
        "band": {"price": 25.0, "as_of": AS_OF, "zone": zone_of(25.0, 10, 20, 40),
                 "option_zone": None, "candidate_zone": None},
        "quote_error": None, "error": None,
    }


def test_option_and_candidate_zones(repo, fetch, quotes):
    # +V band = (10, 25, 50); candidate (36, 50, 60) sits above the price.
    text = framework_md("AAA.US", "Alpha Corp", "`fast_grower`") + (
        "\n### Outputs\n\n"
        "- \\(V_{opt}\\) = **10** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0**\n"
        "- 候选带（T）：L′ = **36** · M′ = **50** · H′ = **60** · 门槛 A9\n"
    )
    (repo / "AAA.US.md").write_text(text, encoding="utf-8")
    fetch.prices["AAA.US"] = 35.0
    band = rows_by_ticker(repo, quotes)["AAA.US"]["band"]
    assert band["zone"] == zone_of(35.0, 10, 20, 40) == "optimistic"
    assert band["option_zone"] == zone_of(35.0, 10, 25, 50) == "neutral"
    assert band["candidate_zone"] == zone_of(35.0, 36, 50, 60) == "deep_discount"


def test_parse_failure_is_one_row_not_whole_list(repo, quotes):
    row = rows_by_ticker(repo, quotes)["0001.HK"]
    assert row["error"]
    assert row["name"] is None and row["band"] is None and row["market"] is None and row["quote_error"] is None


def test_one_batched_fetch_for_parsed_rows(repo, fetch, quotes):
    catalog(repo, quotes)
    assert fetch.calls == [["AAA.US", "BBB.US"]]


def test_quotes_are_reused_up_to_the_max_age(repo, fetch, clock, quotes):
    catalog(repo, quotes)
    clock.now += QUOTE_MAX_AGE_S - 1
    catalog(repo, quotes)
    assert len(fetch.calls) == 1
    clock.now += 2
    fetch.prices["AAA.US"] = 26.0
    band = rows_by_ticker(repo, quotes)["AAA.US"]["band"]
    assert len(fetch.calls) == 2
    assert band["price"] == 26.0
    assert band["as_of"] == datetime.fromtimestamp(clock.now, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def test_refresh_refetches_quotes_older_than_the_refresh_max_age(repo, fetch, clock, quotes):
    catalog(repo, quotes)
    clock.now += QUOTE_REFRESH_MAX_AGE_S - 1
    catalog(repo, quotes, refresh=True)
    assert len(fetch.calls) == 1
    clock.now += 1
    catalog(repo, quotes)  # a plain load still reuses the batch
    assert len(fetch.calls) == 1
    catalog(repo, quotes, refresh=True)
    assert len(fetch.calls) == 2


def test_a_changed_pool_is_refetched(repo, fetch, quotes):
    catalog(repo, quotes)
    (repo / "CCC.US.md").write_text(framework_md("CCC.US", "Gamma", "`stalwart`"), encoding="utf-8")
    catalog(repo, quotes)
    assert len(fetch.calls) == 2


def test_missing_symbol_nulls_only_its_row(repo, fetch, quotes, caplog):
    del fetch.prices["AAA.US"]
    with caplog.at_level(logging.WARNING, logger="lynch_reader"):
        out = catalog(repo, quotes)
    rows = {r["ticker"]: r for r in out["tickers"]}
    assert out["quote_error"] is None
    assert rows["AAA.US"]["band"] is None and rows["AAA.US"]["quote_error"] == NO_QUOTE
    assert rows["BBB.US"]["band"]["price"] == 5.0 and rows["BBB.US"]["quote_error"] is None
    assert any("AAA.US" in r.getMessage() for r in caplog.records)


def test_no_usable_quote_is_logged_once_per_batch(repo, fetch, clock, quotes, caplog):
    del fetch.prices["AAA.US"]
    with caplog.at_level(logging.WARNING, logger="lynch_reader"):
        catalog(repo, quotes)
        catalog(repo, quotes)  # cache hit: same batch, no new warning
        assert sum("AAA.US" in r.getMessage() for r in caplog.records) == 1
        clock.now += QUOTE_MAX_AGE_S
        catalog(repo, quotes)  # a new batch warns again
        assert sum("AAA.US" in r.getMessage() for r in caplog.records) == 2


@pytest.mark.parametrize("price", [float("nan"), float("inf"), 0.0, -1.0], ids=["nan", "inf", "zero", "negative"])
def test_unusable_price_nulls_only_its_row(repo, fetch, quotes, price):
    fetch.prices["AAA.US"] = price
    rows = rows_by_ticker(repo, quotes)
    assert rows["AAA.US"]["band"] is None and rows["AAA.US"]["quote_error"] == NO_QUOTE
    assert rows["BBB.US"]["band"]["price"] == 5.0


@pytest.mark.parametrize("error", [
    RuntimeError("longbridge CLI not found on PATH"),
    RuntimeError("longbridge quote failed (exit 1): token expired"),
    subprocess.TimeoutExpired(["longbridge", "quote"], 15),
], ids=["not-on-path", "non-zero-exit", "timeout"])
def test_whole_call_failure_nulls_every_band(repo, clock, error):
    fetch = FakeFetch({}, error=error)
    out = catalog(repo, QuoteCache(fetch=fetch, monotonic=clock, wall=clock))
    assert out["quote_error"]
    assert all(r["band"] is None for r in out["tickers"])
    assert all(r["quote_error"] is None for r in out["tickers"])


def test_whole_call_failure_backs_off_then_retries(repo, clock):
    fetch = FakeFetch({"AAA.US": 25.0}, error=RuntimeError("boom"))
    quotes = QuoteCache(fetch=fetch, monotonic=clock, wall=clock)
    assert catalog(repo, quotes)["quote_error"] == "取价失败：boom"
    # Within the back-off the failure is reused, even once the CLI would succeed.
    fetch.error = None
    clock.now += QUOTE_BACKOFF_S - 1
    assert catalog(repo, quotes)["quote_error"] == "取价失败：boom"
    assert len(fetch.calls) == 1
    # Past it the call is retried; the failure is never served longer.
    clock.now += 1
    out = catalog(repo, quotes)
    assert out["quote_error"] is None and len(fetch.calls) == 2


def test_failure_after_a_good_batch_does_not_serve_the_old_batch(repo, fetch, clock, quotes):
    catalog(repo, quotes)
    clock.now += QUOTE_MAX_AGE_S
    fetch.error = RuntimeError("boom")
    out = catalog(repo, quotes)
    assert out["quote_error"] and all(r["band"] is None for r in out["tickers"])


def test_requests_waiting_on_a_failing_call_reuse_its_failure(repo, clock):
    started, release = threading.Event(), threading.Event()
    calls: list[list[str]] = []

    def slow_failing_fetch(symbols):
        calls.append(symbols)
        started.set()
        release.wait(5)
        raise RuntimeError("boom")

    quotes = QuoteCache(fetch=slow_failing_fetch, monotonic=clock, wall=clock)
    results: dict[str, dict] = {}
    first = threading.Thread(target=lambda: results.update(first=catalog(repo, quotes)))
    waiter = threading.Thread(target=lambda: results.update(waiter=catalog(repo, quotes)))
    first.start()
    assert started.wait(5)
    waiter.start()  # blocks on the cache lock behind the in-flight call
    release.set()
    first.join(5)
    waiter.join(5)
    assert len(calls) == 1
    assert results["first"]["quote_error"] == results["waiter"]["quote_error"] == "取价失败：boom"


def test_max_age_follows_the_monotonic_clock_not_the_wall_clock(repo, fetch, clock):
    wall = FakeClock()
    quotes = QuoteCache(fetch=fetch, monotonic=clock, wall=wall)
    catalog(repo, quotes)
    wall.now -= 3600  # the wall clock is set back an hour: no effect on the max age
    catalog(repo, quotes)
    assert len(fetch.calls) == 1
    clock.now += QUOTE_MAX_AGE_S
    band = rows_by_ticker(repo, quotes)["AAA.US"]["band"]
    assert len(fetch.calls) == 2
    # `as_of` still reports wall time.
    assert band["as_of"] == datetime.fromtimestamp(wall.now, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def test_out_of_order_option_band_is_one_row(repo, quotes):
    # p_L > p_M passes the parse ([0,1] each) but yields a +V band with L > M.
    text = framework_md("AAA.US", "Alpha Corp", "`fast_grower`") + (
        "\n### Outputs\n\n- \\(V_{opt}\\) = **100** · \\(p_L/p_M/p_H\\) = **1 / 0 / 0**\n"
    )
    (repo / "AAA.US.md").write_text(text, encoding="utf-8")
    rows = rows_by_ticker(repo, quotes)
    assert rows["AAA.US"]["error"] and rows["AAA.US"]["band"] is None
    assert rows["BBB.US"]["band"]["price"] == 5.0


def test_unreadable_framework_is_one_row(repo, quotes):
    (repo / "AAA.US.md").write_bytes(b"\xff\xfe\x00")
    rows = rows_by_ticker(repo, quotes)
    assert rows["AAA.US"]["error"]
    assert rows["BBB.US"]["band"]["price"] == 5.0


def test_framework_path_only_resolves_pool_members(repo):
    fw = repo
    assert framework_path("AAA.US", fw) == fw / "AAA.US.md"
    assert framework_path("_TEMPLATE", fw) is None
    assert framework_path("../AGENTS", fw) is None
    assert framework_path("NOPE.US", fw) is None


ASSUMPTIONS = """
## 2. Assumptions

| # | 假设 | 状态 | 来源 | 输入 |
|---|------|------|------|------|
| A1 | base numerator | `active` | x | y |
| A3′ | **候选带（T）**：only one house \\| escaped pipe, \\(g_1\\) | `active` | x | y |

## 3. 可演算块
"""


def write_candidate(fw, line: str, assumptions: str = ASSUMPTIONS) -> None:
    text = framework_md("AAA.US", "Alpha Corp", "`fast_grower`") + assumptions + "\n### Outputs\n\n" + line + "\n"
    (fw / "AAA.US.md").write_text(text, encoding="utf-8")


def test_candidate_with_gate_row(repo, quotes):
    fw = repo
    write_candidate(fw, "- 候选带（T）：L′ = **11** · M′ = **22** · H′ = **44** · 门槛 A3′（note 门槛 A1）")
    assert rows_by_ticker(fw, quotes)["AAA.US"]["candidate"] == {
        "label": "T", "L": 11.0, "M": 22.0, "H": 44.0, "gate": "A3′",
        "background": "**候选带（T）**：only one house \\| escaped pipe, \\(g_1\\)",
    }


def test_candidate_gate_row_missing(repo, quotes):
    fw = repo
    write_candidate(fw, "- 候选带（T）：L′ = **11** · M′ = **22** · H′ = **44** · 门槛 A9")
    cand = rows_by_ticker(fw, quotes)["AAA.US"]["candidate"]
    assert cand["gate"] == "A9" and cand["background"] is None


def test_candidate_gate_only_counts_in_assumptions(repo, quotes):
    fw = repo
    other = ASSUMPTIONS + "\n| A9 | not an assumption | x | y | z |\n"
    write_candidate(fw, "- 候选带（T）：L′ = **11** · M′ = **22** · H′ = **44** · 门槛 A9", other)
    assert rows_by_ticker(fw, quotes)["AAA.US"]["candidate"]["background"] is None


def test_candidate_without_gate_token(repo, quotes):
    # 600096.SH shape: the label mentions an assumption id, but nothing follows the prices.
    fw = repo
    write_candidate(fw, "- 候选带（FY2027·单家，A3′）：L′ = **11** · M′ = **22** · H′ = **44**；门槛 FY2027 ≥2 家")
    cand = rows_by_ticker(fw, quotes)["AAA.US"]["candidate"]
    assert cand["label"] == "FY2027·单家，A3′"
    assert cand["gate"] is None and cand["background"] is None


def test_no_candidate_is_null(repo, quotes):
    assert rows_by_ticker(repo, quotes)["AAA.US"]["candidate"] is None


@needs_content
def test_real_frameworks_candidates(quotes):
    rows = rows_by_ticker(FRAMEWORKS_DIR, quotes)
    line = re.compile(r"^-\s+(?:\*\*)?候选带（", re.MULTILINE)
    with_line = [p.stem for p in list_framework_paths(FRAMEWORKS_DIR)
                 if line.search(p.read_text(encoding="utf-8"))]
    assert with_line
    for ticker in with_line:
        cand = rows[ticker]["candidate"]
        assert cand is not None, ticker
        assert cand["L"] < cand["M"] < cand["H"], ticker
    # Only these two name no `门槛 A…` after their primed anchors; every gated one resolves its row.
    ungated = {t for t in with_line if rows[t]["candidate"]["gate"] is None}
    assert ungated == {"0189.HK", "600096.SH"}
    assert all(rows[t]["candidate"]["background"] for t in with_line if t not in ungated)
    tencent = rows["0700.HK"]["candidate"]
    assert tencent["gate"] == "A1c"
    assert tencent["background"].startswith("**候选带（FY2028E·汇丰单家）**")


OPTION_ASSUMPTIONS = """
## 2. Assumptions

| # | 假设 | 状态 | 来源 | 输入 |
|---|------|------|------|------|
| A12 | **SiC 走 \\(V_{opt}\\)**：one-off external price | `active` | x | y |
| A2e-7c-4 | option ceiling rule | `active` | x | y |
| A3′ | primed row | `active` | x | y |

## 3. 可演算块
"""
OPTION_LINE = ("\\(V_{opt}\\) = **10** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0** —— "
               "上限见 A12、A2e-7c-4 与 A3′；A2f 与 A99 不在表里，A12 重复")


def write_option(fw, outputs: str, assumptions: str = OPTION_ASSUMPTIONS) -> None:
    text = framework_md("AAA.US", "Alpha Corp", "`fast_grower`") + assumptions + "\n### Outputs\n\n" + outputs
    (fw / "AAA.US.md").write_text(text, encoding="utf-8")


def test_option_band_with_its_background(repo, quotes):
    write_option(repo, f"- {OPTION_LINE}\n")
    assert rows_by_ticker(repo, quotes)["AAA.US"]["option"] == {
        "v_opt": 10.0, "L": 10.0, "M": 25.0, "H": 50.0, "line": OPTION_LINE,
        # Ids in the line's order, once each; only those that head an Assumptions row.
        "rows": [
            {"id": "A12", "text": "**SiC 走 \\(V_{opt}\\)**：one-off external price"},
            {"id": "A2e-7c-4", "text": "option ceiling rule"},
            {"id": "A3′", "text": "primed row"},
        ],
    }


def test_option_without_outputs_line_keeps_the_band(repo, quotes):
    # V_opt and p_k written outside Outputs: the band still shows, the missing text is simply absent.
    elsewhere = OPTION_ASSUMPTIONS + "\n\\(V_{opt}\\) = **10** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0**\n"
    write_option(repo, "- 底牌说明\n", elsewhere)
    option = rows_by_ticker(repo, quotes)["AAA.US"]["option"]
    assert option == {"v_opt": 10.0, "L": 10.0, "M": 25.0, "H": 50.0, "line": None, "rows": []}


@pytest.mark.parametrize("outputs", [
    "- \\(V_{opt}\\) = **0**\n",
    "- \\(V_{opt}\\) = **0** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0**\n",
    "- \\(V_{opt}\\) = **10**（no per-tier credit）\n",
], ids=["zero", "zero-with-p", "no-p"])
def test_no_option_band_is_null(repo, quotes, outputs):
    write_option(repo, outputs)
    assert rows_by_ticker(repo, quotes)["AAA.US"]["option"] is None


@needs_content
def test_real_frameworks_options(quotes):
    rows = rows_by_ticker(FRAMEWORKS_DIR, quotes)
    with_option = {t: r["option"] for t, r in rows.items() if r["option"] is not None}
    assert len(with_option) >= 20
    for ticker, option in with_option.items():
        assert option["v_opt"] > 0, ticker
        # Every credited band today keeps the strict ladder: no ties.
        assert option["L"] < option["M"] < option["H"], ticker
        assert option["line"] and option["line"].startswith("\\(V_{opt}\\)"), ticker
    # A V_opt = 0 disclosure line is no band.
    assert rows["0700.HK"]["option"] is None
    cohr = with_option["COHR.US"]
    assert cohr["v_opt"] == 18.17
    assert [round(cohr[k], 2) for k in "LMH"] == [77.82, 197.51, 331.15]
    assert [r["id"] for r in cohr["rows"]] == ["A12"]
    assert cohr["rows"][0]["text"].startswith("**SiC 子公司不进底牌")
