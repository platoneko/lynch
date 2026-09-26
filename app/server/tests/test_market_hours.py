from __future__ import annotations

from datetime import datetime

import pytest

from lynch_reader.market_hours import MARKET_TZ, traded_between, us_regular_close

# 2026-09-21 is a Monday; 09-25 a Friday; 09-27 a Sunday.


def at(market: str, day: int, hh: int, mm: int = 0) -> float:
    return datetime(2026, 9, day, hh, mm, tzinfo=MARKET_TZ[market]).timestamp()


@pytest.mark.parametrize("market, t0, t1, traded", [
    ("A", (21, 15, 20), (21, 23, 0), False),  # after the close settled
    ("A", (21, 15, 10), (21, 23, 0), True),  # inside the settle grace
    ("A", (21, 23, 0), (22, 9, 20), True),  # opening auction
    ("A", (25, 16, 0), (28, 9, 0), False),  # over a weekend
    ("HK", (21, 16, 5), (21, 20, 0), True),  # closing auction
    ("HK", (21, 16, 30), (22, 8, 50), False),
    ("US", (25, 20, 30), (27, 19, 0), False),  # Friday post-market close to Sunday evening
    ("US", (25, 20, 30), (27, 20, 30), True),  # Monday's overnight session opens Sunday 20:00
    ("US", (22, 21, 0), (22, 23, 0), True),  # weekday overnight session
])
def test_traded_between(market, t0, t1, traded):
    assert traded_between(market, at(market, *t0), at(market, *t1)) is traded


def test_unknown_market_always_counts_as_traded():
    assert traded_between(None, at("US", 26, 12), at("US", 26, 13)) is True


@pytest.mark.parametrize("now, close_day", [
    ((21, 10, 0), 21),  # during Monday's regular session
    ((21, 9, 0), 18),  # Monday pre-market: Friday's session is the latest
    ((26, 12, 0), 25),  # Saturday
    ((25, 18, 0), 25),  # Friday post-market
])
def test_us_regular_close(now, close_day):
    assert us_regular_close(at("US", *now)) == datetime(2026, 9, close_day, 16, 0, tzinfo=MARKET_TZ["US"])
