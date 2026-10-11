from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from eval.lib.frameworks import list_framework_paths
from eval.lib.zones import zone_of
from eval.lib.price import SessionQuote
from lynch_reader import catalog as catalog_module
from lynch_reader.catalog import (
    FRAMEWORKS_DIR, NO_QUOTE, QUOTE_POLL_S, QuotePoller, QuoteCache, axis_path_in, catalog, framework_path,
)
from tests.conftest import NOW, FakeFetch, framework_md

needs_content = pytest.mark.skipif(not FRAMEWORKS_DIR.is_dir(), reason="content repo not cloned at frameworks/")

AS_OF = datetime.fromtimestamp(NOW, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


POOL = ["AAA.US", "BBB.US", "0001.HK"]


def rows_by_ticker(fw, quotes):
    return {r["ticker"]: r for r in catalog(fw, quotes)["tickers"]}


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


def test_parsed_row_is_zoned_on_the_live_price(repo, quotes):
    out = catalog(repo, quotes)
    assert out["quote_error"] is None
    row = {r["ticker"]: r for r in out["tickers"]}["AAA.US"]
    assert row == {
        "ticker": "AAA.US", "name": "Alpha Corp", "market": "US",
        "lynch_primary": "fast_grower", "lynch_secondary": "cyclical", "currency": "USD",
        "L": 10.0, "M": 20.0, "H": 40.0, "option": None, "candidate": None, "axes": [], "ref_axes": [], "consensus": None,
        "band": {"price": 25.0, "session": None, "as_of": AS_OF, "zone": zone_of(25.0, 10, 20, 40),
                 "option_zone": None, "candidate_zone": None},
        "quote_error": None, "error": None, "archived": False,
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
    real = catalog_module.load_framework
    monkeypatch.setattr(catalog_module, "load_framework", lambda p: (parsed.append(p.stem), real(p))[1])
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
    real = catalog_module.load_framework
    monkeypatch.setattr(catalog_module, "load_framework", lambda p: (parsed.append(p.stem), real(p))[1])
    for p in repo.glob("*.md"):
        settle(p)
    (repo / "AAA.US.md").write_text(framework_md("AAA.US", "Alpha Corp", "`stalwart`"), encoding="utf-8")
    catalog(repo, quotes)
    parsed.clear()
    catalog(repo, quotes)
    assert parsed == ["AAA.US"]


def test_axis_files_are_parsed_again_only_when_changed(repo, quotes, monkeypatch):
    _axis(repo, "al", ["AAA.US"])
    parsed: list[str] = []
    real = catalog_module.load_axis
    monkeypatch.setattr(catalog_module, "load_axis", lambda p: (parsed.append(p.stem), real(p))[1])
    settle(repo / "axes" / "al.md")
    catalog(repo, quotes)
    catalog(repo, quotes)
    assert parsed == ["al"]


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
    # Every gated band resolves its assumption row; which tickers are gated is research content.
    assert all(rows[t]["candidate"]["background"] for t in with_line if rows[t]["candidate"]["gate"])


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


def test_option_outside_outputs_is_ignored(repo, quotes):
    # The line contract puts V_opt and p_k in Outputs; prose elsewhere (ORCL.US's old §0) is not an option.
    elsewhere = OPTION_ASSUMPTIONS + "\n\\(V_{opt}\\) = **10** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0**\n"
    write_option(repo, "- 底牌说明\n", elsewhere)
    assert rows_by_ticker(repo, quotes)["AAA.US"]["option"] is None


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
def test_us_row_takes_the_newest_price_since_the_regular_close(repo, fetch, clock, quotes, now, price, session):
    printed = (
        SessionQuote("post", 27.0, datetime.fromtimestamp(ny(25, 17, 30), timezone.utc)),
        SessionQuote("pre", 24.0, datetime.fromtimestamp(ny(28, 6), timezone.utc)),
        SessionQuote("overnight", 26.0, datetime.fromtimestamp(ny(25, 3), timezone.utc)),  # before Friday's open
    )
    # Only trades printed by `now` can be in the quote.
    fetch.extended["AAA.US"] = tuple(x for x in printed if x.at.timestamp() <= now)
    clock.now = now
    band = rows_by_ticker(repo, quotes)["AAA.US"]["band"]
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


def test_axes_and_mounts(repo, quotes):
    _axis(repo, "al", "AAA.US · CCC.US；候选：BBB.US", "> 共识：L（高盛 2027E；as-of 2026-09-03）\n")
    _axis(repo, "cu", "AAA.US")
    (repo / "axes" / "rates.md").write_text(
        "# _AXIS.rates — 无风险利率轴\n> 轴类型：`rates_axis` | 挂载票：AAA.US\n", encoding="utf-8")
    body = catalog(repo, quotes)
    assert [a["name"] for a in body["axes"]] == ["al", "cu"]
    al = body["axes"][0]
    assert al["label"] == "AL 价格"
    assert al["consensus"] == {"tier": "L", "note": "高盛 2027E；as-of 2026-09-03"}
    assert al["tickers"] == ["AAA.US", "CCC.US"]
    assert body["axes"][1]["consensus"] is None
    rows = {r["ticker"]: r for r in body["tickers"]}
    assert rows["AAA.US"]["axes"] == ["al", "cu"]
    assert rows["BBB.US"]["axes"] == []


def test_reference_mounts_are_listed_apart(repo, quotes):
    _axis(repo, "al", "AAA.US ｜ 参照：BBB.US；候选：CCC.US")
    body = catalog(repo, quotes)
    al = body["axes"][0]
    assert (al["tickers"], al["refs"]) == (["AAA.US"], ["BBB.US"])
    rows = {r["ticker"]: r for r in body["tickers"]}
    assert (rows["AAA.US"]["axes"], rows["AAA.US"]["ref_axes"]) == (["al"], [])
    assert (rows["BBB.US"]["axes"], rows["BBB.US"]["ref_axes"]) == ([], ["al"])


def test_bad_axis_is_dropped_alone(repo, quotes, caplog):
    _axis(repo, "al", "AAA.US", "> 共识：高（x）\n")
    _axis(repo, "cu", "AAA.US")
    body = catalog(repo, quotes)
    assert [a["name"] for a in body["axes"]] == ["cu"]
    assert "axis parse failed: al.md" in caplog.text


def test_ticker_consensus_line(repo, quotes):
    md = framework_md("BBB.US", "Beta Inc", "`stalwart`").replace(
        "> 主尺：test ruler", "> 主尺：test ruler\n> 共识：H（卖方 2027；as-of 2026-09-20）")
    (repo / "BBB.US.md").write_text(md, encoding="utf-8")
    rows = {r["ticker"]: r for r in catalog(repo, quotes)["tickers"]}
    assert rows["BBB.US"]["consensus"] == {"tier": "H", "note": "卖方 2027；as-of 2026-09-20"}
    assert rows["AAA.US"]["consensus"] is None


def test_axis_path_only_resolves_axis_files(repo):
    _axis(repo, "al", "AAA.US")
    assert axis_path_in("al", repo) == repo / "axes" / "al.md"
    assert axis_path_in("../AAA.US", repo) is None
    assert axis_path_in("nope", repo) is None
