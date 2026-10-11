from __future__ import annotations

import json
from datetime import datetime

import pytest

from lynch_reader.earnings import RETRY_S, CalendarCache, Earnings, IgnoreStore, judge, release_at
from lynch_reader.market_hours import MARKET_TZ
from eval.lib.earnings_lb import CalendarEvent, CalendarFetch  # after lynch_reader, which puts the repo root on sys.path

# 2026-10-14 is a Wednesday.


def ev(day: str, slot: str = "after", period: str = "2026 财年第 3 季度") -> CalendarEvent:
    return CalendarEvent(day, slot, period)


def at(market: str, month: int, day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=MARKET_TZ[market])


@pytest.mark.parametrize("ticker, slot, local", [
    ("TSM.US", "before", (9, 30)),
    ("TSM.US", "during", (16, 0)),
    ("TSM.US", "after", (16, 0)),
    ("TSM.US", "unknown", (16, 0)),
    ("0700.HK", "before", (9, 30)),
    ("0700.HK", "after", (16, 0)),
    ("600036.SH", "before", (9, 30)),
    ("600036.SH", "after", (15, 0)),
    ("000933.SZ", "during", (15, 0)),
])
def test_release_moment_by_slot_and_market(ticker, slot, local):
    market = {"US": "US", "HK": "HK", "SH": "A", "SZ": "A"}[ticker.rsplit(".", 1)[1]]
    assert release_at(ticker, ev("2026-10-14", slot)) == at(market, 10, 14, *local)


def test_unknown_market_has_no_moment():
    assert release_at("D05.SG", ev("2026-10-14")) is None
    assert judge("D05.SG", [ev("2026-10-14")], at("US", 10, 14, 12)) is None


@pytest.mark.parametrize("slot, before_flip, after_flip", [
    ("before", (9, 29), (9, 30)),
    ("after", (15, 59), (16, 0)),
    ("during", (15, 59), (16, 0)),
    ("unknown", (15, 59), (16, 0)),
])
def test_flips_to_released_at_the_moment(slot, before_flip, after_flip):
    events = [ev("2026-10-14", slot)]
    assert judge("TSM.US", events, at("US", 10, 14, *before_flip)).status == "upcoming"
    assert judge("TSM.US", events, at("US", 10, 14, *after_flip)).status == "released"


def test_a_share_after_close_releases_at_15_beijing():
    events = [ev("2026-10-20", "after")]
    assert judge("300750.SZ", events, at("A", 10, 20, 14, 59)).status == "upcoming"
    assert judge("300750.SZ", events, at("A", 10, 20, 15, 0)).status == "released"


def test_judged_in_the_markets_own_day():
    # 2026-10-15 01:00 Beijing is still 10-14 13:00 in New York: TSM's 10-14 after-close release is today, upcoming.
    row = judge("TSM.US", [ev("2026-10-14", "after")], at("A", 10, 15, 1))
    assert (row.status, row.day_offset) == ("upcoming", 0)


def test_upcoming_window_is_three_days():
    now = at("US", 10, 11, 12)
    assert judge("TSM.US", [ev("2026-10-14")], now).day_offset == 3
    assert judge("TSM.US", [ev("2026-10-15")], now) is None


def test_released_window_is_seven_days():
    events = [ev("2026-10-14", "after")]
    assert judge("TSM.US", events, at("US", 10, 21, 23)).day_offset == -7
    assert judge("TSM.US", events, at("US", 10, 22, 0)) is None


def test_two_entries_make_one_row():
    # The calendar lists a quarter and the half-year report a day apart.
    events = [ev("2026-07-27", "after", "2026 财年第 2 季度"), ev("2026-07-28", "after", "2026 财年半年报")]
    row = judge("000933.SZ", events, at("A", 7, 28, 10))
    assert (row.status, row.event.period) == ("released", "2026 财年第 2 季度")
    row = judge("000933.SZ", events, at("A", 7, 28, 16))
    assert (row.status, row.event.period) == ("released", "2026 财年半年报")


def test_released_wins_over_upcoming_and_nearest_upcoming_wins():
    assert judge("TSM.US", [ev("2026-10-14"), ev("2026-10-16")], at("US", 10, 14, 17)).event.date == "2026-10-14"
    assert judge("TSM.US", [ev("2026-10-17"), ev("2026-10-15")], at("US", 10, 14, 12)).event.date == "2026-10-15"


# ---- calendar cache, ignores, research, response


class Clock:
    def __init__(self, t: datetime) -> None:
        self.now = t.timestamp()

    def __call__(self) -> float:
        return self.now

    def set(self, t: datetime) -> None:
        self.now = t.timestamp()


class FakeCalendar:
    """Stands in for `fetch_calendar`: fixed events, a call log, and optional failing tickers or a raise."""

    def __init__(self, events: dict[str, list[CalendarEvent]]) -> None:
        self.events, self.calls, self.fail, self.error = events, [], set(), None

    def __call__(self, tickers, start, end) -> CalendarFetch:
        self.calls.append((list(tickers), start, end))
        if self.error is not None:
            raise self.error
        failed = [t for t in tickers if t in self.fail]
        # Like the CLI: releases before `start` are not returned.
        since = {t: [e for e in self.events.get(t, []) if e.date >= start.isoformat()] for t in tickers}
        return CalendarFetch({t: since[t] for t in tickers if t not in failed}, failed,
                             "longbridge failed (exit 1): token expired" if failed else None)


@pytest.fixture
def cal():
    return FakeCalendar({"TSM.US": [ev("2026-10-14", "after")], "0700.HK": [ev("2026-11-12", "after")]})


def test_one_full_fetch_per_beijing_day(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    events, day, error = cache.get(["TSM.US", "0700.HK"])
    assert (day, error) == ("2026-10-14", None)
    assert events["TSM.US"] == [ev("2026-10-14", "after")]
    _, start, end = cal.calls[0]
    assert (start.isoformat(), end.isoformat()) == ("2026-10-06", "2026-11-13")  # 8 days back: see the US test
    clk.set(at("A", 10, 14, 23, 59))
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 1
    clk.set(at("A", 10, 15, 0, 1))
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 2 and cal.calls[1][0] == ["TSM.US", "0700.HK"]


def test_new_ticker_is_fetched_alone_and_removed_one_filtered(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    cache.get(["TSM.US"])
    events, _, _ = cache.get(["TSM.US", "0700.HK"])
    assert cal.calls[1][0] == ["0700.HK"] and "0700.HK" in events
    events, _, _ = cache.get(["0700.HK"])
    assert len(cal.calls) == 2 and set(events) == {"0700.HK"}


def test_ticker_without_events_is_not_refetched(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    cache.get(["NOPE.US"])
    events, _, _ = cache.get(["NOPE.US"])
    assert events == {"NOPE.US": []} and len(cal.calls) == 1


def test_cache_survives_a_restart(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    events, day, _ = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    assert len(cal.calls) == 1 and day == "2026-10-14" and events["TSM.US"][0].slot == "after"


def test_corrupt_cache_file_starts_empty(tmp_path, cal):
    (tmp_path / "calendar.json").write_text("{not json", encoding="utf-8")
    clk = Clock(at("A", 10, 14, 8))
    events, _, _ = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    assert events["TSM.US"] and len(cal.calls) == 1


def test_failure_keeps_old_events_and_backs_off(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    cache.get(["TSM.US", "0700.HK"])
    clk.set(at("A", 10, 15, 8))
    cal.fail = {"TSM.US"}
    events, day, error = cache.get(["TSM.US", "0700.HK"])
    assert day == "2026-10-14" and "token expired" in error
    assert events["TSM.US"] == [ev("2026-10-14", "after")]  # yesterday's events kept
    clk.now += RETRY_S - 1
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 2  # inside the back-off: no call
    cal.fail = set()
    clk.now += 1
    events, day, error = cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 3 and (day, error) == ("2026-10-15", None)


def test_fetch_that_raises_is_a_failure_not_a_crash(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cal.error = RuntimeError("boom")
    events, day, error = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    assert (events, day) == ({"TSM.US": []}, None) and "boom" in error


def test_ignore_is_per_release_and_pruned(tmp_path):
    clk = Clock(at("A", 10, 14, 8))
    store = IgnoreStore(tmp_path / "ignored.json", wall=clk)
    store.ignore("TSM.US", "2026-10-14")
    store.ignore("TSM.US", "2026-10-14")
    assert store.keys() == {("TSM.US", "2026-10-14")}
    store.unignore("TSM.US", "2026-10-14")
    assert store.keys() == set()
    store.ignore("AAA.US", "2026-10-10")
    assert store.keys() == {("AAA.US", "2026-10-10")}  # still inside the window on 10-14
    clk.set(at("A", 10, 20, 8))
    store.ignore("BBB.US", "2026-10-20")  # writing prunes keys past the released window
    assert store.keys() == {("BBB.US", "2026-10-20")}
    assert json.loads((tmp_path / "ignored.json").read_text()) == [["BBB.US", "2026-10-20"]]


def snapshot(tmp_path, cal, now: datetime, chat_starts=(), ignore=()):
    clk = Clock(now)
    store = IgnoreStore(tmp_path / "ignored.json", wall=clk)
    for key in ignore:
        store.ignore(*key)
    e = Earnings(CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk), store, wall=clk)
    return e.snapshot(["TSM.US", "0700.HK"], list(chat_starts))


def test_snapshot_shape(tmp_path, cal):
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17))
    assert body["fetched_day"] == "2026-10-15" and body["error"] is None  # 17:00 New York is 05:00 next day Beijing
    assert body["items"] == [{
        "ticker": "TSM.US", "status": "released", "date": "2026-10-14", "day_offset": 0, "slot": "after",
        "release_at": "2026-10-14T16:00:00-04:00", "period": "2026 财年第 3 季度",
        "researched": False, "ignored": False,
    }]


def test_chat_before_release_is_not_research(tmp_path, cal):
    release = at("US", 10, 14, 16).timestamp()
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17), chat_starts=[("TSM.US", release - 60)])
    assert body["items"][0]["researched"] is False
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17), chat_starts=[("TSM.US", release), ("0700.HK", release)])
    assert body["items"][0]["researched"] is True


def test_ignored_release_is_flagged(tmp_path, cal):
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17), ignore=[("TSM.US", "2026-10-14")])
    assert body["items"][0]["ignored"] is True


def test_us_release_is_fetched_through_its_seventh_local_day(tmp_path):
    # Beijing 10-21 10:00 is New York 10-20 22:00: a 10-13 release is New York's 7th day, Beijing's 8th.
    cal = FakeCalendar({"TSM.US": [ev("2026-10-13", "after")]})
    clk = Clock(at("A", 10, 21, 10))
    e = Earnings(CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk), IgnoreStore(tmp_path / "i.json", wall=clk),
                 wall=clk)
    items = e.snapshot(["TSM.US"], [])["items"]
    assert [(i["status"], i["day_offset"]) for i in items] == [("released", -7)]


def test_paired_entries_keep_research_and_ignore(tmp_path):
    # The quarter and the half-year report a day apart are one release: handling the first covers the second.
    cal = FakeCalendar({"000933.SZ": [ev("2026-07-27", "after", "2026 财年第 2 季度"),
                                      ev("2026-07-28", "after", "2026 财年半年报")]})
    clk = Clock(at("A", 7, 28, 16))
    store = IgnoreStore(tmp_path / "i.json", wall=clk)
    e = Earnings(CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk), store, wall=clk)
    first = at("A", 7, 27, 15).timestamp()
    item = e.snapshot(["000933.SZ"], [("000933.SZ", first + 60)])["items"][0]
    assert (item["period"], item["researched"]) == ("2026 财年半年报", True)
    store.ignore("000933.SZ", "2026-07-27")
    item = e.snapshot(["000933.SZ"], [])["items"][0]
    assert item["ignored"] is True
    store.unignore("000933.SZ", item["date"])  # the page sends the row's own (latest) date
    assert e.snapshot(["000933.SZ"], [])["items"][0]["ignored"] is False


def test_released_newest_first_then_upcoming_soonest_first(tmp_path):
    cal = FakeCalendar({
        "AAA.US": [ev("2026-10-12", "after")], "BBB.US": [ev("2026-10-13", "after")],
        "CCC.US": [ev("2026-10-16", "after")], "DDD.US": [ev("2026-10-15", "before")],
    })
    clk = Clock(at("US", 10, 14, 12))
    e = Earnings(CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk), IgnoreStore(tmp_path / "i.json", wall=clk),
                 wall=clk)
    items = e.snapshot(["AAA.US", "BBB.US", "CCC.US", "DDD.US"], [])["items"]
    assert [i["ticker"] for i in items] == ["BBB.US", "AAA.US", "DDD.US", "CCC.US"]
