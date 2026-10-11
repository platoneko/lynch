"""Earnings list for the app: which pool tickers just released or are about to release (AGENTS.md「app 对话」).

The calendar comes from longbridge (eval/lib/earnings_lb.py). A release counts as out once its session slot's
moment has passed on the market's own clock: before-open releases at the open, everything else at the close.
Nothing waits for report data to reach longbridge.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import Any

from lynch_reader.catalog import REPO_ROOT  # also puts the repo root on sys.path for eval.lib
from lynch_reader.daily_cache import BJT, RETRY_S, DailyTickerCache, TickerFetch, write_json  # noqa: F401 — RETRY_S re-exported for tests
from lynch_reader.market_hours import MARKET_TZ

from eval.lib.earnings_lb import CalendarEvent, CalendarFetch, fetch_calendar  # noqa: E402
from eval.lib.frameworks import market_bucket  # noqa: E402

RELEASED_DAYS = 7  # a release stays listed through its 7th local day
UPCOMING_DAYS = 3  # an upcoming release is listed from 3 local days before

OPEN = clock(9, 30)
# A mid-session release has no known time: the close is the moment it surely is out.
CLOSE = {"A": clock(15, 0), "HK": clock(16, 0), "US": clock(16, 0)}

EARNINGS_DIR = REPO_ROOT / "output" / "earnings"
CALENDAR_FILE = EARNINGS_DIR / "calendar.json"
IGNORED_FILE = EARNINGS_DIR / "ignored.json"
LOOKAHEAD_DAYS = 30  # passed as --end; the CLI returns later releases anyway
# Beijing, every day: A and HK have closed, the US regular session is hours away, and the HK baselines' 16:23
# kline run is done, so the two never hit longbridge together.
CALENDAR_RUN = clock(16, 37)

Fetch = Callable[[list[str], date, date], CalendarFetch]


@dataclass(frozen=True)
class Row:
    ticker: str
    status: str  # released | upcoming
    event: CalendarEvent
    release_at: datetime
    day_offset: int  # release date minus the market's local today
    # Every released entry in the window is one release (the calendar pairs a quarter with its half-year report a
    # day apart): research counts from the earliest of them, and an ignore on any of their dates covers the row.
    since: datetime
    dates: tuple[str, ...]


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
            released.append(Row(ticker, "released", ev, moment, offset, moment, (ev.date,)))
        elif moment > now and offset <= UPCOMING_DAYS:
            upcoming.append(Row(ticker, "upcoming", ev, moment, offset, moment, (ev.date,)))
    if released:
        latest = max(released, key=lambda r: r.release_at)
        return replace(latest, since=min(r.release_at for r in released),
                       dates=tuple(sorted(r.event.date for r in released)))
    if upcoming:
        return min(upcoming, key=lambda r: r.release_at)
    return None


class CalendarCache(DailyTickerCache[list[CalendarEvent]]):
    """The pool's earnings calendar, refreshed by the policy in daily_cache.py."""

    def __init__(self, path: Path, fetch: Fetch = fetch_calendar, wall: Callable[[], float] = time.time) -> None:
        self._calendar = fetch
        super().__init__(path, wall)

    def get(self, pool: list[str], *,
            refresh: bool = True) -> tuple[dict[str, list[CalendarEvent]], str | None, str | None]:
        """(events per pool ticker, Beijing day of the last full fetch, last error)."""
        events, error = super().get(pool, refresh=refresh)
        at = self._last_full_fetch("all")
        day = None if at is None else datetime.fromtimestamp(at, BJT).date().isoformat()
        return {t: events.get(t, []) for t in pool}, day, error

    def _group(self, ticker: str) -> str:
        return "all"

    def _run_at(self, group: str, now: float) -> float:
        t = datetime.fromtimestamp(now, BJT)
        run = datetime.combine(t.date(), CALENDAR_RUN, BJT)
        return (run if run <= t else run - timedelta(days=1)).timestamp()

    def _fetch(self, tickers: list[str], today: date) -> TickerFetch[list[CalendarEvent]]:
        # One extra day back: a US market's local day trails Beijing's, so its 7th day is Beijing's 8th.
        start, end = today - timedelta(days=RELEASED_DAYS + 1), today + timedelta(days=LOOKAHEAD_DAYS)
        got = self._calendar(tickers, start, end)
        return TickerFetch(got.events, got.failed, got.error)

    def _encode(self, value: list[CalendarEvent]) -> Any:
        return [{"date": e.date, "slot": e.slot, "period": e.period} for e in value]

    def _decode(self, raw: Any) -> list[CalendarEvent]:
        return [CalendarEvent(**e) for e in raw]


class IgnoreStore:
    """Releases the user dismissed, as (ticker, release date) pairs in a sorted JSON list.

    The server is the only writer and calls it from async handlers on one event loop, so no lock.
    """

    def __init__(self, path: Path, wall: Callable[[], float] = time.time) -> None:
        self._path, self._wall = path, wall

    def keys(self) -> set[tuple[str, str]]:
        if not self._path.exists():
            return set()
        return {(t, d) for t, d in json.loads(self._path.read_text(encoding="utf-8"))}

    def ignore(self, ticker: str, day: str) -> None:
        self._write(self.keys() | {(ticker, day)})

    def unignore(self, ticker: str, day: str) -> None:
        # The page sends its row's latest date; the ignore may sit on an earlier entry of the same release.
        oldest = (date.fromisoformat(day) - timedelta(days=RELEASED_DAYS)).isoformat()
        self._write({(t, d) for t, d in self.keys() if not (t == ticker and oldest <= d <= day)})

    def _write(self, keys: set[tuple[str, str]]) -> None:
        # A key past the released window can no longer match a listed row.
        oldest = (datetime.fromtimestamp(self._wall(), BJT).date() - timedelta(days=RELEASED_DAYS + 1)).isoformat()
        write_json(self._path, sorted([t, d] for t, d in keys if d >= oldest))


class Earnings:
    """The `GET /api/earnings` body: judged rows with research and ignore flags."""

    def __init__(self, calendar: CalendarCache, ignored: IgnoreStore, wall: Callable[[], float] = time.time) -> None:
        self.calendar, self.ignored, self._wall = calendar, ignored, wall

    def snapshot(self, pool: list[str], chat_starts: list[tuple[str, float]],
                 tag: Callable[[str], dict[str, Any] | None] = lambda t: None) -> dict[str, Any]:
        events, day, error = self.calendar.get(pool)
        now = datetime.fromtimestamp(self._wall(), BJT)
        ignored = self.ignored.keys()
        rows = [r for t, evs in events.items() if (r := judge(t, evs, now)) is not None]
        released = sorted((r for r in rows if r.status == "released"), key=lambda r: r.release_at, reverse=True)
        upcoming = sorted((r for r in rows if r.status == "upcoming"), key=lambda r: r.release_at)
        return {"fetched_day": day, "error": error,
                "items": [_item(r, ignored, chat_starts, tag(r.ticker)) for r in released + upcoming]}


def _item(r: Row, ignored: set[tuple[str, str]], chat_starts: list[tuple[str, float]],
          anomaly: dict[str, Any] | None) -> dict[str, Any]:
    since = r.since.timestamp()
    return {
        "ticker": r.ticker, "status": r.status, "date": r.event.date, "day_offset": r.day_offset,
        "slot": r.event.slot, "release_at": r.release_at.isoformat(), "period": r.event.period,
        # Any 讨论 chat on the ticker started after the release counts, wherever it was opened from.
        "researched": any(t == r.ticker and created >= since for t, created in chat_starts),
        "ignored": any((r.ticker, d) in ignored for d in r.dates),
        "anomaly": anomaly,  # the ticker's anomaly filed under earnings (anomalies.py), if any
    }
