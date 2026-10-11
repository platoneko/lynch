"""Earnings list for the app: which pool tickers just released or are about to release (AGENTS.md「app 对话」).

The calendar comes from longbridge (eval/lib/earnings_lb.py). A release counts as out once its session slot's
moment has passed on the market's own clock: before-open releases at the open, everything else at the close.
Nothing waits for report data to reach longbridge.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as clock

from lynch_reader.catalog import REPO_ROOT  # noqa: F401 — also puts the repo root on sys.path for eval.lib
from lynch_reader.market_hours import MARKET_TZ

from eval.lib.earnings_lb import CalendarEvent  # noqa: E402
from eval.lib.frameworks import market_bucket  # noqa: E402

RELEASED_DAYS = 7  # a release stays listed through its 7th local day
UPCOMING_DAYS = 3  # an upcoming release is listed from 3 local days before

OPEN = clock(9, 30)
# A mid-session release has no known time: the close is the moment it surely is out.
CLOSE = {"A": clock(15, 0), "HK": clock(16, 0), "US": clock(16, 0)}


@dataclass(frozen=True)
class Row:
    ticker: str
    status: str  # released | upcoming
    event: CalendarEvent
    release_at: datetime
    day_offset: int  # release date minus the market's local today


def release_at(ticker: str, ev: CalendarEvent) -> datetime | None:
    market = market_bucket(ticker)
    if market not in CLOSE:
        return None
    moment = OPEN if ev.slot == "before" else CLOSE[market]
    return datetime.combine(date.fromisoformat(ev.date), moment, MARKET_TZ[market])


def judge(ticker: str, events: list[CalendarEvent], now: datetime) -> Row | None:
    """The ticker's one row: its latest release inside the released window, else its nearest upcoming one."""
    released: list[Row] = []
    upcoming: list[Row] = []
    for ev in events:
        moment = release_at(ticker, ev)
        if moment is None:
            return None
        offset = (moment.date() - now.astimezone(moment.tzinfo).date()).days
        if moment <= now and -offset <= RELEASED_DAYS:
            released.append(Row(ticker, "released", ev, moment, offset))
        elif moment > now and offset <= UPCOMING_DAYS:
            upcoming.append(Row(ticker, "upcoming", ev, moment, offset))
    if released:
        return max(released, key=lambda r: r.release_at)
    if upcoming:
        return min(upcoming, key=lambda r: r.release_at)
    return None
