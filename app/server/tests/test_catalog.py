from __future__ import annotations

import logging
import copy
import os
import subprocess
import threading
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from eval.lib.zones import zone_of
from eval.lib.price import SessionQuote
from lynch_reader import catalog as catalog_module
from lynch_reader.catalog import (
    FRAMEWORKS_DIR, NO_QUOTE, QUOTE_POLL_S, QuotePoller, QuoteCache, axis_path_in, catalog, framework_path,
    framework_updates,
)
from tests.conftest import NOW, FakeFetch, framework_md

AS_OF = datetime.fromtimestamp(NOW, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


POOL = ["AAA.US", "BBB.US", "0001.HK"]


class SyntheticResults:
    """Confirmed synthetic outputs; quote and document code never runs a model."""
    def __init__(self):
        self.values = {
            ticker: {'status': 'confirmed', 'review_id': f'review-{ticker}', 'output': {
                'ticker': ticker, 'currency': 'USD', 'units': {'price': 'USD_PER_SHARE'},
                'anchors': {'L': 10, 'M': 20, 'H': 40}, 'candidate': None, 'option': None,
            }} for ticker in ('AAA.US', 'BBB.US')
        }

    def current(self, ticker):
        return copy.deepcopy(self.values.get(ticker, {'status': 'missing'}))


@pytest.fixture
def results():
    return SyntheticResults()


def rows_by_ticker(fw, quotes, results=None):
    return {r["ticker"]: r for r in catalog(fw, quotes, valuation_provider=results)["tickers"]}


def next_poll(clock, quotes):
    clock.now += QUOTE_POLL_S  # NOW is inside US trading hours: the poll fetches
    quotes.poll(POOL)


def test_labels_come_from_eval_lib(repo, quotes):
    out = catalog(repo, quotes)
    assert [z["id"] for z in out["zones"]] == ["deep_discount", "conservative", "neutral", "optimistic", "stretched"]
    assert out["zones"][1]["label"] == "保守"
    assert {"id": "fast_grower", "label": "快速增长"} in out["lynch_classes"]


def test_pool_order_and_template_excluded(repo, quotes):
    assert [r["ticker"] for r in catalog(repo, quotes)["tickers"]] == ["AAA.US", "BBB.US", "0001.HK"]


def test_archived_rows_are_marked_not_dropped(repo, quotes):
    rows = {r["ticker"]: r for r in catalog(repo, quotes, archived={"AAA.US", "0001.HK", "GONE.US"})["tickers"]}
    assert {t: r["archived"] for t, r in rows.items()} == {"AAA.US": True, "BBB.US": False, "0001.HK": True}
    assert rows["AAA.US"]["band"]["price"] == 25.0
    assert rows["0001.HK"]["error"]  # a broken framework can be put away too


def test_parsed_row_is_zoned_on_the_live_price(repo, quotes, results):
    out = catalog(repo, quotes, valuation_provider=results)
    assert out["quote_error"] is None
    row = {r["ticker"]: r for r in out["tickers"]}["AAA.US"]
    expected = {
        "ticker": "AAA.US", "name": "Alpha Corp", "market": "US",
        "lynch_primary": "fast_grower", "lynch_secondary": "cyclical", "currency": "USD",
        "L": 10.0, "M": 20.0, "H": 40.0, "option": None, "candidate": None, "axes": [], "ref_axes": [], "consensus": None,
        "band": {"price": 25.0, "session": None, "as_of": AS_OF, "zone": zone_of(25.0, 10, 20, 40),
                 "option_zone": None, "candidate_zone": None},
        "quote_error": None, "error": None, "archived": False,
        "framework_updated": "2026-09-21", "last_earnings": None, "next_earnings": None, "stale_after_earnings": False,
    }
    assert {key: row[key] for key in expected} == expected


def test_earnings_dates_are_asked_with_each_frameworks_header_day(repo, quotes):
    asked = []

    def dates(updated, versions):
        asked.append(dict(updated))
        # A broken framework is still in the pool, so the calendar answers for it too.
        return {t: {"last_earnings": {"date": "2026-09-18"}, "stale_after_earnings": True} for t in ("AAA.US", "0001.HK")}
    rows = {r["ticker"]: r for r in catalog(repo, quotes, earnings_dates=dates)["tickers"]}
    assert asked == [{"AAA.US": date(2026, 9, 21), "BBB.US": date(2026, 9, 21)}]
    assert (rows["AAA.US"]["last_earnings"], rows["AAA.US"]["stale_after_earnings"]) == ({"date": "2026-09-18"}, True)
    assert (rows["BBB.US"]["last_earnings"], rows["BBB.US"]["stale_after_earnings"]) == (None, False)
    assert rows["0001.HK"]["stale_after_earnings"] is False  # a broken framework is never flagged


def test_earnings_header_date_does_not_depend_on_valuation_in_document(repo, quotes):
    (repo / "AAA.US.md").write_text(
        "# AAA.US — Alpha Corp\n\n"
        "> 林奇：`fast_grower` | 更新：2026-09-21 | eval: eval/AAA.US.py\n"
        "> 主尺：按业务估值\n\n"
        "## 估值区间\n\n情景估值由脚本计算。\n",
        encoding="utf-8",
    )
    assert framework_updates(repo)["AAA.US"] == date(2026, 9, 21)
    def dates(updated, versions):
        assert updated["AAA.US"] == date(2026, 9, 21)
        return {"AAA.US": {"last_earnings": {"date": "2026-09-18"}, "stale_after_earnings": True}}

    row = {r["ticker"]: r for r in catalog(repo, quotes, earnings_dates=dates)["tickers"]}["AAA.US"]
    assert row["name"] == "Alpha Corp"
    assert row["lynch_primary"] == "fast_grower"
    assert row["framework_updated"] == "2026-09-21"
    assert row["last_earnings"] == {"date": "2026-09-18"}
    assert row["band"]["price"] == 25
    assert row["band"]["zone"] is None


def test_earnings_uses_same_document_version_as_the_catalog_row(repo, quotes, monkeypatch):
    original_read = quotes.read
    asked = []

    def change_file_during_quote_read(symbols):
        document = framework_md("AAA.US", "Alpha Corp", "`fast_grower` / `cyclical`")
        (repo / "AAA.US.md").write_text(document.replace("2026-09-21", "2026-10-21"), encoding="utf-8")
        return original_read(symbols)

    def dates(updated, versions):
        asked.append(dict(updated))
        return {}

    monkeypatch.setattr(quotes, "read", change_file_during_quote_read)
    rows = {r["ticker"]: r for r in catalog(repo, quotes, earnings_dates=dates)["tickers"]}
    assert asked[0]["AAA.US"].isoformat() == rows["AAA.US"]["framework_updated"]


def test_catalog_parses_the_document_bytes_it_already_read(repo, quotes, monkeypatch):
    real_parse = catalog_module.parse_framework_identity

    def change_file_before_parse(md, ticker):
        if ticker == "AAA.US":
            (repo / 'AAA.US.md').write_text(framework_md("AAA.US", "Changed", "`stalwart`"), encoding="utf-8")
        return real_parse(md, ticker)

    monkeypatch.setattr(catalog_module, "parse_framework_identity", change_file_before_parse)
    assert rows_by_ticker(repo, quotes)["AAA.US"]["name"] == "Alpha Corp"


def test_option_and_candidate_zones(repo, fetch, quotes, results):
    # +V band = (10, 25, 50); candidate (36, 50, 60) sits above the price.
    text = framework_md("AAA.US", "Alpha Corp", "`fast_grower`") + (
        "\n### Outputs\n\n"
        "- \\(V_{opt}\\) = **10** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0**\n"
        "- 候选带（T）：L′ = **36** · M′ = **50** · H′ = **60** · 门槛 A9\n"
    )
    (repo / "AAA.US.md").write_text(text, encoding="utf-8")
    results.values['AAA.US']['output']['option'] = {
        'value': 10, 'value_unit': 'USD_PER_SHARE', 'anchors': {'L': 10, 'M': 25, 'H': 50}}
    results.values['AAA.US']['output']['candidate'] = {
        'label': 'T', 'anchors': {'L': 36, 'M': 50, 'H': 60}}
    fetch.prices["AAA.US"] = 35.0
    band = rows_by_ticker(repo, quotes, results)["AAA.US"]["band"]
    assert band["zone"] == zone_of(35.0, 10, 20, 40) == "optimistic"
    assert band["option_zone"] == zone_of(35.0, 10, 25, 50) == "neutral"
    assert band["candidate_zone"] == zone_of(35.0, 36, 50, 60) == "deep_discount"


def test_parse_failure_is_one_row_not_whole_list(repo, quotes):
    row = rows_by_ticker(repo, quotes)["0001.HK"]
    assert row["error"]
    assert row["name"] is None and row["band"] is None and row["market"] is None and row["quote_error"] is None


def test_one_batched_fetch_for_every_framework(repo, fetch, quotes):
    catalog(repo, quotes)
    catalog(repo, quotes)  # a plain load only reads
    assert fetch.calls == [["AAA.US", "BBB.US", "0001.HK"]]


def test_loads_show_the_latest_polled_batch(repo, fetch, clock, quotes):
    catalog(repo, quotes)
    clock.now += QUOTE_POLL_S
    fetch.prices["AAA.US"] = 26.0
    assert rows_by_ticker(repo, quotes)["AAA.US"]["band"]["price"] == 25.0  # a load never fetches
    quotes.poll(POOL)
    assert len(fetch.calls) == 2
    band = rows_by_ticker(repo, quotes)["AAA.US"]["band"]
    assert band["price"] == 26.0
    assert band["as_of"] == datetime.fromtimestamp(clock.now, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def test_poll_fetches_when_the_pool_gains_a_ticker(fetch, clock, quotes):
    clock.now = ny(26, 12)  # Saturday: no market trades
    quotes.poll(["AAA.US"])
    quotes.poll(["AAA.US", "BBB.US"])
    assert fetch.calls == [["AAA.US"], ["AAA.US", "BBB.US"]]


def test_a_new_framework_is_fetched_by_the_next_read(repo, fetch, quotes):
    catalog(repo, quotes)
    (repo / "CCC.US.md").write_text(framework_md("CCC.US", "Gamma", "`stalwart`"), encoding="utf-8")
    fetch.prices["CCC.US"] = 30.0
    assert rows_by_ticker(repo, quotes)["CCC.US"]["band"]["price"] == 30.0
    assert len(fetch.calls) == 2 and "CCC.US" in fetch.calls[1]  # the whole pool, as the poll would ask
    catalog(repo, quotes)
    assert len(fetch.calls) == 2


def test_a_failed_fetch_for_a_new_framework_is_left_to_the_poll(repo, fetch, clock, quotes):
    catalog(repo, quotes)
    (repo / "CCC.US.md").write_text(framework_md("CCC.US", "Gamma", "`stalwart`"), encoding="utf-8")
    fetch.error = RuntimeError("boom")
    catalog(repo, quotes)
    row = rows_by_ticker(repo, quotes)["CCC.US"]  # reads do not retry the CLI on every request
    assert row["band"] is None and row["quote_error"] == "等待下一次取价"
    assert len(fetch.calls) == 2
    fetch.error = None
    fetch.prices["CCC.US"] = 30.0
    clock.now += QUOTE_POLL_S
    quotes.poll(list(fetch.calls[1]))
    assert rows_by_ticker(repo, quotes)["CCC.US"]["band"]["price"] == 30.0


def settle(path, seconds_ago: float = 60.0) -> None:
    """Age a file's mtime past the window in which an edit could keep the same timestamp."""
    t = time.time() - seconds_ago
    os.utime(path, (t, t))


def test_only_changed_files_are_parsed_again(repo, quotes, monkeypatch):
    parsed: list[str] = []
    real = catalog_module.parse_framework_identity
    monkeypatch.setattr(catalog_module, "parse_framework_identity", lambda md, ticker: (parsed.append(ticker), real(md, ticker))[1])
    for p in repo.glob("*.md"):
        settle(p)
    catalog(repo, quotes)
    assert sorted(parsed) == ["0001.HK", "AAA.US", "BBB.US"]

    parsed.clear()
    catalog(repo, quotes)
    assert parsed == []

    (repo / "AAA.US.md").write_text(framework_md("AAA.US", "Alpha Renamed", "`stalwart`"), encoding="utf-8")
    settle(repo / "AAA.US.md", seconds_ago=30.0)
    (repo / "BBB.US.md").unlink()
    rows = rows_by_ticker(repo, quotes)
    assert parsed == ["AAA.US"]
    assert rows["AAA.US"]["name"] == "Alpha Renamed" and "BBB.US" not in rows


def test_a_just_written_file_is_parsed_on_every_read(repo, quotes, monkeypatch):
    # A same-size rewrite inside one timestamp tick keeps mtime and size: a fresh file is never trusted.
    parsed: list[str] = []
    real = catalog_module.parse_framework_identity
    monkeypatch.setattr(catalog_module, "parse_framework_identity", lambda md, ticker: (parsed.append(ticker), real(md, ticker))[1])
    for p in repo.glob("*.md"):
        settle(p)
    (repo / "AAA.US.md").write_text(framework_md("AAA.US", "Alpha Corp", "`stalwart`"), encoding="utf-8")
    catalog(repo, quotes)
    parsed.clear()
    catalog(repo, quotes)
    assert parsed == ["AAA.US"]


def test_axis_documents_do_not_supply_homepage_relationships(repo, quotes, results):
    _axis(repo, "al", ["AAA.US"])
    body = catalog(repo, quotes, valuation_provider=results)
    row = {item['ticker']: item for item in body['tickers']}['AAA.US']
    assert row['valuation_status'] == 'confirmed' and row['M'] == 20
    assert row['axis_status'] == 'unknown' and row['axes'] == []
    assert body['axes'] == []


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
        catalog(repo, quotes)  # same batch, no new warning
        assert sum("AAA.US" in r.getMessage() for r in caplog.records) == 1
        next_poll(clock, quotes)  # a new batch warns again
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
    out = catalog(repo, QuoteCache(fetch=fetch, wall=clock))
    assert out["quote_error"]
    assert all(r["band"] is None for r in out["tickers"])
    assert all(r["quote_error"] is None for r in out["tickers"])


def test_failure_keeps_the_last_good_batch(repo, fetch, clock, quotes):
    catalog(repo, quotes)
    fetch.error = RuntimeError("boom")
    next_poll(clock, quotes)
    out = catalog(repo, quotes)
    shown = datetime.fromtimestamp(NOW, ZoneInfo("Asia/Shanghai")).strftime("%H:%M")  # the first batch's time
    assert out["quote_error"] == f"取价失败：boom（显示的是 {shown} 的现价）"
    assert {r["ticker"]: r for r in out["tickers"]}["AAA.US"]["band"]["price"] == 25.0
    fetch.error = None
    next_poll(clock, quotes)
    assert catalog(repo, quotes)["quote_error"] is None


def test_listener_hears_each_new_batch_and_cannot_break_it(repo, fetch, clock, quotes):
    heard = []
    quotes.subscribe(lambda b: heard.append(b.quotes["AAA.US"].last))
    quotes.subscribe(lambda b: 1 / 0)  # a broken listener is logged, not raised
    catalog(repo, quotes)
    catalog(repo, quotes)
    assert heard == [25.0]
    assert rows_by_ticker(repo, quotes)["AAA.US"]["band"]["price"] == 25.0


def test_poller_round_polls_then_runs_upkeep_and_survives_errors(fetch, clock, quotes):
    order = []
    quotes.subscribe(lambda b: order.append("batch"))
    poller = QuotePoller(quotes, lambda: ["AAA.US"], after=lambda: order.append("upkeep"))
    poller.run_once()
    assert order == ["batch", "upkeep"]
    broken = QuotePoller(quotes, lambda: 1 / 0, after=lambda: order.append("upkeep"))
    broken.run_once()  # a failing round is logged; upkeep still runs
    assert order[-1] == "upkeep"


def test_poller_thread_runs_a_round_at_start_and_stops(fetch, quotes):
    ran = threading.Event()
    poller = QuotePoller(quotes, lambda: ["AAA.US"], after=ran.set, interval_s=3600)
    poller.start()
    assert ran.wait(5)
    poller.stop()
    assert len(fetch.calls) == 1


def test_requests_waiting_on_a_failing_call_reuse_its_failure(repo, clock):
    started, release = threading.Event(), threading.Event()
    calls: list[list[str]] = []

    def slow_failing_fetch(symbols):
        calls.append(symbols)
        started.set()
        release.wait(5)
        raise RuntimeError("boom")

    quotes = QuoteCache(fetch=slow_failing_fetch, wall=clock)
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


def test_out_of_order_option_band_is_one_row(repo, quotes, results):
    # An invalid result must not affect a different stock or hide its quote.
    results.values['AAA.US']['output']['option'] = {
        'value': 100, 'value_unit': 'USD_PER_SHARE', 'anchors': {'L': 110, 'M': 20, 'H': 40}}
    rows = rows_by_ticker(repo, quotes, results)
    assert rows["AAA.US"]["valuation_status"] == 'invalid'
    assert rows["AAA.US"]["band"]["price"] == 25
    assert rows["AAA.US"]["band"]["zone"] is None
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


def test_candidate_values_and_label_come_from_saved_result(repo, quotes, results):
    fw = repo
    write_candidate(fw, "- 候选带（T）：L′ = **11** · M′ = **22** · H′ = **44** · 门槛 A3′（note 门槛 A1）")
    results.values['AAA.US']['output']['candidate'] = {'label': '产能', 'anchors': {'L': 12, 'M': 23, 'H': 45}}
    assert rows_by_ticker(fw, quotes, results)["AAA.US"]["candidate"] == {
        "label": "产能", "L": 12, "M": 23, "H": 45}


def test_candidate_unavailable_does_not_display_band(repo, quotes, results):
    fw = repo
    write_candidate(fw, "- 候选带（T）：L′ = **11** · M′ = **22** · H′ = **44** · 门槛 A9")
    results.values['AAA.US']['output']['candidate'] = {'status': 'unavailable'}
    row = rows_by_ticker(fw, quotes, results)["AAA.US"]
    assert row['candidate'] is None
    assert row['candidate_review_status'] == 'unavailable'
    assert row['band']['candidate_zone'] is None


def test_document_gate_does_not_override_saved_candidate(repo, quotes, results):
    fw = repo
    other = ASSUMPTIONS + "\n| A9 | not an assumption | x | y | z |\n"
    write_candidate(fw, "- 候选带（T）：L′ = **11** · M′ = **22** · H′ = **44** · 门槛 A9", other)
    results.values['AAA.US']['output']['candidate'] = {'label': '单家', 'anchors': {'L': 11, 'M': 22, 'H': 44}}
    cand = rows_by_ticker(fw, quotes, results)["AAA.US"]["candidate"]
    assert cand == {'label': '单家', 'L': 11, 'M': 22, 'H': 44}
    assert 'gate' not in cand and 'background' not in cand


def test_candidate_label_is_preserved_without_interpreting_gate_tokens(repo, quotes, results):
    # 600096.SH shape: the label mentions an assumption id, but nothing follows the prices.
    fw = repo
    write_candidate(fw, "- 候选带（FY2027·单家，A3′）：L′ = **11** · M′ = **22** · H′ = **44**；门槛 FY2027 ≥2 家")
    results.values['AAA.US']['output']['candidate'] = {
        'label': 'FY2027·单家，A3′', 'anchors': {'L': 11, 'M': 22, 'H': 44}}
    cand = rows_by_ticker(fw, quotes, results)["AAA.US"]["candidate"]
    assert cand["label"] == "FY2027·单家，A3′"
    assert 'gate' not in cand and 'background' not in cand


def test_no_candidate_is_null(repo, quotes, results):
    write_candidate(repo, '- 候选带（T）：L′ = **11** · M′ = **22** · H′ = **44**')
    row = rows_by_ticker(repo, quotes, results)['AAA.US']
    assert row['valuation_status'] == 'confirmed' and row['M'] == 20
    assert row['candidate'] is None and row['band']['candidate_zone'] is None


def test_candidate_bands_come_from_each_saved_result_and_validate_order(repo, quotes, results):
    results.values['AAA.US']['output']['candidate'] = {'label': '扩产', 'anchors': {'L': 11, 'M': 22, 'H': 44}}
    results.values['BBB.US']['output']['candidate'] = {'label': '单家', 'anchors': {'L': 15, 'M': 14, 'H': 45}}
    rows = rows_by_ticker(repo, quotes, results)
    assert rows['AAA.US']['candidate'] == {'label': '扩产', 'L': 11, 'M': 22, 'H': 44}
    assert rows['AAA.US']['band']['candidate_zone'] == zone_of(25, 11, 22, 44)
    assert rows['BBB.US']['valuation_status'] == 'invalid'
    assert rows['BBB.US']['band']['price'] == 5
    assert rows['BBB.US']['band']['candidate_zone'] is None


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


def test_option_band_comes_from_saved_result_not_document_formula(repo, quotes, results):
    write_option(repo, f"- {OPTION_LINE}\n")
    results.values['AAA.US']['output']['option'] = {
        'label': '新产线', 'value': 8, 'value_unit': 'USD_PER_SHARE',
        'anchors': {'L': 10, 'M': 24, 'H': 48}}
    assert rows_by_ticker(repo, quotes, results)["AAA.US"]["option"] == {
        "v_opt": 8, "L": 10, "M": 24, "H": 48, "label": '新产线'}


def test_option_outside_outputs_is_ignored(repo, quotes, results):
    # The line contract puts V_opt and p_k in Outputs; prose elsewhere (ORCL.US's old §0) is not an option.
    elsewhere = OPTION_ASSUMPTIONS + "\n\\(V_{opt}\\) = **10** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0**\n"
    write_option(repo, "- 底牌说明\n", elsewhere)
    row = rows_by_ticker(repo, quotes, results)['AAA.US']
    assert row['valuation_status'] == 'confirmed' and row['M'] == 20
    assert row['option'] is None and row['band']['option_zone'] is None


@pytest.mark.parametrize("outputs", [
    "- \\(V_{opt}\\) = **0**\n",
    "- \\(V_{opt}\\) = **0** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0**\n",
    "- \\(V_{opt}\\) = **10**（no per-tier credit）\n",
], ids=["zero", "zero-with-p", "no-p"])
def test_no_option_band_is_null(repo, quotes, results, outputs):
    write_option(repo, outputs)
    row = rows_by_ticker(repo, quotes, results)['AAA.US']
    assert row['valuation_status'] == 'confirmed' and row['M'] == 20
    assert row['option'] is None and row['band']['option_zone'] is None


def test_option_bands_use_saved_values_and_zero_credit_hides_band(repo, quotes, results):
    results.values['AAA.US']['output']['option'] = {
        'value': 10, 'value_unit': 'USD_PER_SHARE', 'anchors': {'L': 10, 'M': 25, 'H': 50}}
    results.values['BBB.US']['output']['option'] = {
        'value': 0, 'value_unit': 'USD_PER_SHARE', 'anchors': {'L': 10, 'M': 20, 'H': 40}}
    rows = rows_by_ticker(repo, quotes, results)
    assert rows['AAA.US']['option'] == {'L': 10, 'M': 25, 'H': 50, 'v_opt': 10}
    assert rows['AAA.US']['band']['option_zone'] == zone_of(25, 10, 25, 50)
    assert rows['BBB.US']['option'] is None
    assert rows['BBB.US']['band']['option_zone'] is None


def ny(day: int, hh: int, mm: int = 0) -> float:
    # 2026-09-21 is a Monday; 09-25 a Friday.
    return datetime(2026, 9, day, hh, mm, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_poll_fetches_only_after_a_market_traded(fetch, clock, quotes):
    # US-only pool. Friday's post-market has settled; Monday's overnight opens Sunday 20:00.
    pool = ["AAA.US", "BBB.US"]
    clock.now = ny(25, 20, 30)
    quotes.poll(pool)
    clock.now = ny(26, 12)
    quotes.poll(pool)
    assert len(fetch.calls) == 1
    clock.now = ny(27, 20, 30)
    quotes.poll(pool)
    assert len(fetch.calls) == 2


@pytest.mark.parametrize("now, price, session", [
    (ny(25, 18), 27.0, "post"),  # post-market after Friday's close
    (ny(26, 12), 27.0, "post"),  # the weekend keeps Friday's post-market price
    (ny(28, 7), 24.0, "pre"),  # Monday pre-market is newer than Friday's post-market
    (ny(28, 10), 25.0, None),  # Monday's regular session has opened: extended prices are older
])
def test_us_row_takes_the_newest_price_since_the_regular_close(repo, fetch, clock, quotes, results, now, price, session):
    printed = (
        SessionQuote("post", 27.0, datetime.fromtimestamp(ny(25, 17, 30), timezone.utc)),
        SessionQuote("pre", 24.0, datetime.fromtimestamp(ny(28, 6), timezone.utc)),
        SessionQuote("overnight", 26.0, datetime.fromtimestamp(ny(25, 3), timezone.utc)),  # before Friday's open
    )
    # Only trades printed by `now` can be in the quote.
    fetch.extended["AAA.US"] = tuple(x for x in printed if x.at.timestamp() <= now)
    clock.now = now
    band = rows_by_ticker(repo, quotes, results)["AAA.US"]["band"]
    assert (band["price"], band["session"]) == (price, session)
    assert band["zone"] == zone_of(price, 10, 20, 40)


def _axis(repo, name, mounts, extra=""):
    d = repo / "axes"
    d.mkdir(exist_ok=True)
    (d / f"{name}.md").write_text(
        f"# _AXIS.{name} — {name.upper()} 价格情景轴（说明）\n"
        f"> 轴类型：`shared_scenario_axis` | 更新：2026-09-27 | 挂载票：{mounts}\n{extra}",
        encoding="utf-8",
    )


def test_axes_and_mounts(repo, quotes, results):
    _axis(repo, "al", "AAA.US · CCC.US；候选：BBB.US", "> 共识：L（高盛 2027E；as-of 2026-09-03）\n")
    _axis(repo, "cu", "AAA.US")
    (repo / "axes" / "rates.md").write_text(
        "# _AXIS.rates — 无风险利率轴\n> 轴类型：`rates_axis` | 挂载票：AAA.US\n", encoding="utf-8")
    body = catalog(repo, quotes, valuation_provider=results)
    assert body["axes"] == []
    rows = {r["ticker"]: r for r in body["tickers"]}
    assert all(rows[ticker]['valuation_status'] == 'confirmed' and rows[ticker]['M'] == 20
               for ticker in ('AAA.US', 'BBB.US'))
    assert rows["AAA.US"]["axes"] == []
    assert rows["AAA.US"]["axis_status"] == 'unknown'
    assert rows["BBB.US"]["axes"] == []


def test_document_reference_mounts_do_not_supply_axis_relations(repo, quotes, results):
    _axis(repo, "al", "AAA.US ｜ 参照：BBB.US；候选：CCC.US")
    body = catalog(repo, quotes, valuation_provider=results)
    assert body['axes'] == []
    rows = {r["ticker"]: r for r in body["tickers"]}
    assert all(rows[ticker]['valuation_status'] == 'confirmed' and rows[ticker]['M'] == 20
               for ticker in ('AAA.US', 'BBB.US'))
    assert rows['AAA.US']['axis_status'] == 'unknown'
    assert rows['BBB.US']['axis_status'] == 'unknown'


def test_bad_saved_axis_is_dropped_without_losing_good_axis_or_band(repo, quotes, results):
    _axis(repo, "al", "AAA.US", "> 共识：高（x）\n")
    _axis(repo, "cu", "AAA.US")
    results.values['AAA.US']['output'].update(
        source_versions={'axis:bad': 'old-bad', 'axis:good': 'old-good'},
        axis_metadata={
            'bad': {'axis_type': 'shared_scenario_axis', 'axis_id': 'bad'},
            'good': {'axis_type': 'shared_scenario_axis', 'axis_id': 'cu', 'logical_id': '_AXIS.cu',
                     'title': '铜价情景轴', 'label': '铜价', 'role': 'priced',
                     'priced_mounts': ['AAA.US'], 'reference_mounts': [], 'historical_consensus': None},
        })
    body = catalog(repo, quotes, valuation_provider=results)
    row = {item['ticker']: item for item in body['tickers']}['AAA.US']
    assert row['valuation_status'] == 'confirmed' and row['M'] == 20
    assert row['axis_status'] == 'unknown' and row['axes'] == ['cu']
    assert [axis['name'] for axis in body['axes']] == ['cu']


def test_ticker_consensus_line(repo, quotes, results):
    md = framework_md("BBB.US", "Beta Inc", "`stalwart`").replace(
        "> 主尺：test ruler", "> 主尺：test ruler\n> 共识：H（卖方 2027；as-of 2026-09-20）")
    (repo / "BBB.US.md").write_text(md, encoding="utf-8")
    rows = {r["ticker"]: r for r in catalog(repo, quotes, valuation_provider=results)["tickers"]}
    assert rows['BBB.US']['valuation_status'] == 'confirmed' and rows['BBB.US']['M'] == 20
    assert rows["BBB.US"]["consensus"] is None
    assert rows["BBB.US"]["axis_status"] == 'unknown'
    assert rows["AAA.US"]["consensus"] is None


def test_axis_path_only_resolves_axis_files(repo):
    _axis(repo, "al", "AAA.US")
    assert axis_path_in("al", repo) == repo / "axes" / "al.md"
    assert axis_path_in("../AAA.US", repo) is None
    assert axis_path_in("nope", repo) is None


def hk(day: int, hh: int, mm: int = 0) -> float:
    return datetime(2026, 9, day, hh, mm, tzinfo=ZoneInfo("Asia/Hong_Kong")).timestamp()


@pytest.mark.parametrize("now, price", [
    (hk(22, 9, 10), 10.0),  # pre-opening session: the last trade is still the previous close
    (hk(22, 9, 30), 8.5),
])
def test_an_opening_auction_price_is_no_trade(clock, now, price):
    fetch = FakeFetch({"0001.HK": 8.5, "0002.HK": 8.5, "AAA.US": 25.0})
    fetch.prev = {"0001.HK": 10.0, "AAA.US": 24.0}
    clock.now = now
    batch = QuoteCache(fetch=fetch, wall=clock).read(["0001.HK", "0002.HK", "AAA.US"]).batch
    assert batch.quotes["0001.HK"].last == price
    assert ("0002.HK" in batch.quotes) is (price == 8.5)  # no previous close to fall back to: no price at all
    assert batch.quotes["AAA.US"].last == 25.0
