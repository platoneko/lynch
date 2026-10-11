from __future__ import annotations

from datetime import datetime

import pytest

from lynch_reader.earnings import judge, release_at
from lynch_reader.market_hours import MARKET_TZ
from eval.lib.earnings_lb import CalendarEvent  # after lynch_reader, which puts the repo root on sys.path

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
