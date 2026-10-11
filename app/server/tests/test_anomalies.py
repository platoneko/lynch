from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from lynch_reader.anomalies import Anomalies, AnomalyStore, Baseline, BaselineCache, Hit, baseline_from_bars, hits
from lynch_reader.catalog import QuoteBatch
from lynch_reader.earnings import CalendarCache
from lynch_reader.market_hours import MARKET_TZ
from eval.lib.earnings_lb import CalendarEvent, CalendarFetch  # after lynch_reader: repo root on sys.path
from eval.lib.price import DailyBar, Quote, SessionQuote
from tests.conftest import NOW

BASE = Baseline(sigma=0.01, volume=1000.0)


def weekdays_until(last: date, n: int) -> list[date]:
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def us_bars(n: int = 62, last: date = date(2026, 9, 18), volume: float = 1000.0) -> list[DailyBar]:
    """Closes alternating ±1%: sigma 0.01. US bars sit at 04:00 UTC = local midnight."""
    out, close = [], 100.0
    for i, d in enumerate(weekdays_until(last, n)):
        if i:
            close *= 1.01 if i % 2 else 0.99
        out.append(DailyBar(datetime(d.year, d.month, d.day, 4, tzinfo=timezone.utc), close, volume))
    return out


NY_NOW = datetime.fromtimestamp(NOW, MARKET_TZ["US"])  # Mon 2026-09-21 10:13


# ---- baseline

def test_baseline_from_closed_bars():
    b = baseline_from_bars("AAA.US", us_bars(), NY_NOW)
    assert b.sigma == pytest.approx(0.01, rel=1e-6) and b.volume == 1000.0


def test_todays_open_bar_is_left_out():
    bars = us_bars(61) + [DailyBar(datetime(2026, 9, 21, 4, tzinfo=timezone.utc), 500.0, 9e9)]
    b = baseline_from_bars("AAA.US", bars, NY_NOW)
    assert b.sigma == pytest.approx(0.01, rel=1e-6) and b.volume == 1000.0


def test_fewer_than_61_closed_bars_is_no_baseline():
    assert baseline_from_bars("AAA.US", us_bars(60), NY_NOW) is None


def test_hk_bars_are_dated_in_hong_kong():
    # HK bars carry local midnight as 16:00 UTC the day before; a UTC date would drop Friday's bar as "today".
    days = weekdays_until(date(2026, 9, 18), 61)
    bars = [DailyBar(datetime(d.year, d.month, d.day, tzinfo=MARKET_TZ["HK"]).astimezone(timezone.utc),
                     100.0 * (1.01 if i % 2 else 1.0), 5.0) for i, d in enumerate(days)]
    # Friday 09-18 in Hong Kong is still "today" until its midnight: judged on Sat 01:00 HK, all 61 count.
    assert baseline_from_bars("0700.HK", bars, datetime(2026, 9, 19, 1, tzinfo=MARKET_TZ["HK"])) is not None
    assert baseline_from_bars("0700.HK", bars, datetime(2026, 9, 18, 23, tzinfo=MARKET_TZ["HK"])) is None


class FakeBars:
    def __init__(self, bars):
        self.bars, self.calls, self.fail = bars, [], set()

    def __call__(self, symbol, count):
        self.calls.append((symbol, count))
        if symbol in self.fail:
            raise RuntimeError("longbridge kline failed (exit 1): token expired")
        return self.bars


def test_baseline_cache_once_per_day_and_records_too_short(tmp_path):
    fake = FakeBars(us_bars())
    cache = BaselineCache(tmp_path / "baseline.json", fetch=fake, wall=lambda: NOW, sleep=lambda s: None)
    got, day, error = cache.get(["AAA.US", "BBB.US"])
    assert got["AAA.US"].sigma == pytest.approx(0.01, rel=1e-6) and error is None
    assert [c[1] for c in fake.calls] == [62, 62]
    cache.get(["AAA.US", "BBB.US"])
    assert len(fake.calls) == 2
    fake.bars = us_bars(10)
    cache2 = BaselineCache(tmp_path / "b2.json", fetch=fake, wall=lambda: NOW, sleep=lambda s: None)
    assert cache2.get(["CCC.US"])[0] == {"CCC.US": None}  # fetched, too short: never judged, not refetched today


def test_baseline_cache_failure_keeps_others(tmp_path):
    fake = FakeBars(us_bars())
    fake.fail = {"BBB.US"}
    got, _, error = BaselineCache(tmp_path / "baseline.json", fetch=fake, wall=lambda: NOW,
                                  sleep=lambda s: None).get(["AAA.US", "BBB.US"])
    assert "AAA.US" in got and "BBB.US" not in got and "token expired" in error


# ---- judging one quote

def q(last, prev=100.0, volume=None, extended=()):
    return Quote("AAA.US", last, prev, "Normal", extended, volume)


def test_four_times_the_usual_move_fires_and_3_9_does_not():
    [(ref, hit)] = hits(q(104.0), BASE, NOW)
    assert ref == "100.0" and (hit.reason, hit.session, round(hit.mult, 6)) == ("move", "regular", 4.0)
    assert hits(q(103.9), BASE, NOW) == []
    assert hits(q(96.0), BASE, NOW)[0][1].move == pytest.approx(-0.04)


def test_volume_rule_only_in_the_regular_session():
    [(_, hit)] = hits(q(102.5, volume=5000.0), BASE, NOW)
    assert (hit.reason, hit.volume_mult) == ("volume", 5.0)
    assert hits(q(102.5, volume=4999.0), BASE, NOW) == []
    assert hits(q(101.9, volume=9000.0), BASE, NOW) == []  # under 2x the usual move
    post = SessionQuote("post", 102.5, datetime(2026, 9, 21, 21, tzinfo=timezone.utc), 100.0)
    assert hits(q(100.0, volume=9000.0, extended=(post,)), BASE, NOW) == []


def test_extended_price_is_measured_from_its_own_base_close():
    # WOLF 10-07: closed 31.37, post-market printed 40.50 (+29%, sigma 7%).
    post = SessionQuote("post", 40.5, datetime(2026, 10, 7, 20, 5, tzinfo=timezone.utc), 31.37)
    [(ref, hit)] = hits(Quote("WOLF.US", 31.37, 31.0, "Normal", (post,), 1.0), Baseline(0.07, 1e6), NOW)
    assert ref == "31.37" and hit.session == "post" and hit.at == post.at.timestamp()
    assert hit.mult == pytest.approx(0.291 / 0.07, rel=1e-2)


def test_missing_base_or_bad_price_is_skipped():
    assert hits(q(104.0, prev=None), BASE, NOW) == []
    assert hits(q(float("nan")), BASE, NOW) == []


# ---- events

def hit(move, at=NOW, session="regular", reason="move", vm=None):
    return Hit(move, abs(move) / 0.01, session, at, reason, vm)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def test_one_event_per_base_close_with_the_peak_kept(tmp_path):
    clk = Clock()
    store = AnomalyStore(tmp_path / "events.json", wall=clk)
    store.record([("AAA.US", "100.0", hit(0.05))], lambda t: False)
    clk.now += 300
    store.record([("AAA.US", "100.0", hit(0.08)), ("AAA.US", "100.0", hit(0.06))], lambda t: True)
    [e] = store.events()
    assert e.first.move == 0.05 and e.peak.move == 0.08 and e.first_at == NOW and e.last_hit == NOW + 300
    assert e.earnings is False  # decided once, at the first trigger
    store.record([("AAA.US", "108.0", hit(-0.05))], lambda t: False)
    assert len(store.events()) == 2


def test_a_stale_quote_stays_one_event_and_is_kept_while_hit(tmp_path):
    clk = Clock()
    store = AnomalyStore(tmp_path / "events.json", wall=clk)
    for _ in range(10):  # a suspended ticker's last move, seen every day for ten days
        store.record([("AAA.US", "100.0", hit(0.2))], lambda t: False)
        clk.now += 86400
    assert len(store.events()) == 1
    clk.now += 8 * 86400  # no longer hit for over 7 days: dropped
    store.record([], lambda t: False)
    assert store.events() == []


def test_events_and_ignores_survive_a_restart(tmp_path):
    store = AnomalyStore(tmp_path / "events.json", wall=lambda: NOW)
    store.record([("AAA.US", "100.0", hit(0.05))], lambda t: False)
    store.set_ignored("AAA.US", "100.0", True)
    again = AnomalyStore(tmp_path / "events.json", wall=lambda: NOW)
    again.record([("AAA.US", "100.0", hit(0.05))], lambda t: False)
    [e] = again.events()
    assert e.ignored is True and e.first_at == NOW


def test_unknown_event_cannot_be_ignored(tmp_path):
    with pytest.raises(KeyError):
        AnomalyStore(tmp_path / "events.json", wall=lambda: NOW).set_ignored("AAA.US", "1.0", True)


def test_corrupt_files_start_empty(tmp_path):
    (tmp_path / "events.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "baseline.json").write_text("[1, 2", encoding="utf-8")
    assert AnomalyStore(tmp_path / "events.json", wall=lambda: NOW).events() == []
    cache = BaselineCache(tmp_path / "baseline.json", fetch=FakeBars(us_bars()), wall=lambda: NOW, sleep=lambda s: None)
    assert cache.get(["AAA.US"], refresh=False) == ({}, None, None)


# ---- the facade

def calendar(tmp_path, events=None):
    def fetch(tickers, start, end):
        return CalendarFetch({t: (events or {}).get(t, []) for t in tickers}, [], None)
    return CalendarCache(tmp_path / "calendar.json", fetch=fetch, wall=lambda: NOW)


def facade(tmp_path, clk, cal=None):
    baselines = BaselineCache(tmp_path / "baseline.json", fetch=FakeBars(us_bars()), wall=clk, sleep=lambda s: None)
    a = Anomalies(AnomalyStore(tmp_path / "events.json", wall=clk), baselines, cal or calendar(tmp_path), wall=clk)
    a.upkeep(["AAA.US", "BBB.US"])
    return a


def batch(prices: dict[str, float], at=NOW):
    return QuoteBatch(tuple(prices), {t: Quote(t, p, 100.0, "Normal", (), None) for t, p in prices.items()}, at)


def test_observe_judges_only_the_given_pool(tmp_path):
    clk = Clock()
    a = facade(tmp_path, clk)
    a.observe(batch({"AAA.US": 110.0, "BBB.US": 120.0}), ["AAA.US"])  # BBB archived: left out of the pool
    items = a.snapshot(["AAA.US", "BBB.US"], [], None)["items"]
    assert [(i["ticker"], i["session"], i["reason"]) for i in items] == [("AAA.US", "regular", "move")]
    assert items[0]["move"] == pytest.approx(0.1) and items[0]["mult"] == pytest.approx(10.0, rel=1e-3)


def test_newest_event_per_ticker_and_pending_flags(tmp_path):
    clk = Clock()
    a = facade(tmp_path, clk)
    a.observe(batch({"AAA.US": 110.0}), ["AAA.US"])
    a.store.set_ignored("AAA.US", "100.0", True)
    clk.now += 86400
    # Next day: a new base close, a new event; the ignored one is no longer shown.
    a.observe(QuoteBatch(("AAA.US",), {"AAA.US": Quote("AAA.US", 99.0, 110.0, "Normal", (), None)}, clk.now),
              ["AAA.US"])
    [item] = a.snapshot(["AAA.US"], [], None)["items"]
    assert (item["ref"], item["ignored"], item["researched"]) == ("110.0", False, False)
    [item] = a.snapshot(["AAA.US"], [("AAA.US", clk.now + 1)], None)["items"]
    assert item["researched"] is True
    [item] = a.snapshot(["AAA.US"], [("AAA.US", clk.now - 1)], None)["items"]  # a chat from before it fired
    assert item["researched"] is False


def test_listed_for_seven_days_after_it_first_fired(tmp_path):
    clk = Clock()
    a = facade(tmp_path, clk)
    a.observe(batch({"AAA.US": 110.0}), ["AAA.US"])
    clk.now += 7 * 86400
    assert len(a.snapshot(["AAA.US"], [], None)["items"]) == 1
    clk.now += 1
    assert a.snapshot(["AAA.US"], [], None)["items"] == []


def test_ticker_on_the_earnings_list_files_its_anomaly_there(tmp_path):
    clk = Clock()
    # AAA released before Monday's open (NOW is Mon 10:13 New York): on the earnings list.
    cal = calendar(tmp_path, {"AAA.US": [CalendarEvent("2026-09-21", "before", "2026 财年第 3 季度")]})
    a = facade(tmp_path, clk, cal)
    a.observe(batch({"AAA.US": 110.0, "BBB.US": 110.0}), ["AAA.US", "BBB.US"])
    assert [i["ticker"] for i in a.snapshot(["AAA.US", "BBB.US"], [], None)["items"]] == ["BBB.US"]
    tag = a.earnings_tag("AAA.US")
    assert tag["move"] == pytest.approx(0.1) and "researched" not in tag
    assert a.earnings_tag("BBB.US") is None


def test_snapshot_reports_quote_or_baseline_errors(tmp_path):
    clk = Clock()
    a = facade(tmp_path, clk)
    assert a.snapshot(["AAA.US"], [], "取价失败：boom")["error"] == "取价失败：boom"


def test_item_shape(tmp_path):
    clk = Clock()
    a = facade(tmp_path, clk)
    a.observe(batch({"AAA.US": 110.0}), ["AAA.US"])
    [item] = a.snapshot(["AAA.US"], [], None)["items"]
    assert set(item) == {"ticker", "ref", "first_at", "move", "mult", "session", "at", "reason", "volume_mult",
                         "researched", "ignored"}
    assert item["first_at"] == datetime.fromtimestamp(NOW, MARKET_TZ["A"]).isoformat(timespec="seconds")
