"""Earnings list for the app: which pool tickers just released or are about to release (AGENTS.md「app 对话」).

The calendar comes from longbridge (eval/lib/earnings_lb.py). A release counts as out once its session slot's
moment has passed on the market's own clock: before-open releases at the open, everything else at the close.
Nothing waits for report data to reach longbridge.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import Any

from lynch_reader.catalog import REPO_ROOT  # also puts the repo root on sys.path for eval.lib
from lynch_reader.market_hours import MARKET_TZ

from eval.lib.earnings_lb import CalendarEvent, CalendarFetch, fetch_calendar  # noqa: E402
from eval.lib.frameworks import market_bucket  # noqa: E402

log = logging.getLogger(__name__)

RELEASED_DAYS = 7  # a release stays listed through its 7th local day
UPCOMING_DAYS = 3  # an upcoming release is listed from 3 local days before

OPEN = clock(9, 30)
# A mid-session release has no known time: the close is the moment it surely is out.
CLOSE = {"A": clock(15, 0), "HK": clock(16, 0), "US": clock(16, 0)}

EARNINGS_DIR = REPO_ROOT / "output" / "earnings"
CALENDAR_FILE = EARNINGS_DIR / "calendar.json"
IGNORED_FILE = EARNINGS_DIR / "ignored.json"
RETRY_S = 30 * 60  # after a failed fetch; the home page asks every 5 minutes
LOOKAHEAD_DAYS = 30  # passed as --end; the CLI returns later releases anyway
BJT = MARKET_TZ["A"]  # the fetch day is a Beijing day: the user's day

Fetch = Callable[[list[str], date, date], CalendarFetch]


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


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # read on every request: never leave it torn


class CalendarCache:
    """The pool's earnings calendar: fetched in full at most once per Beijing day, persisted across restarts.

    Between full fetches, tickers it has never seen (a new framework) are fetched alone; tickers that left the
    pool are only filtered. Any failure keeps the old events, holds the day open for a full refetch, and is
    not retried for RETRY_S. The lock makes concurrent requests wait for one fetch.
    """

    def __init__(self, path: Path, fetch: Fetch = fetch_calendar, wall: Callable[[], float] = time.time) -> None:
        self._path, self._fetch, self._wall = path, fetch, wall
        self._lock = threading.Lock()
        self._day, self._events = self._load()
        self._failed_at: float | None = None
        self._error: str | None = None

    def get(self, pool: list[str]) -> tuple[dict[str, list[CalendarEvent]], str | None, str | None]:
        with self._lock:
            now = self._wall()
            today = datetime.fromtimestamp(now, BJT).date()
            if self._failed_at is None or now - self._failed_at >= RETRY_S:
                if self._day != today.isoformat():
                    self._refresh(pool, today, now, full=True)
                elif new := [t for t in pool if t not in self._events]:
                    self._refresh(new, today, now, full=False)
            return {t: self._events.get(t, []) for t in pool}, self._day, self._error

    def _refresh(self, tickers: list[str], today: date, now: float, *, full: bool) -> None:
        start, end = today - timedelta(days=RELEASED_DAYS), today + timedelta(days=LOOKAHEAD_DAYS)
        try:
            got = self._fetch(tickers, start, end)
        except Exception as e:  # noqa: BLE001 — the fetcher reports CLI failures itself; this is the backstop
            log.warning("earnings calendar fetch raised: %s", e)
            got = CalendarFetch({}, list(tickers), str(e))
        self._events.update(got.events)
        if got.failed:
            self._failed_at = now
            self._error = f"{got.error}（{len(got.failed)} 只票未更新）"
        else:
            self._failed_at, self._error = None, None
            if full:
                self._day = today.isoformat()
        self._save()

    def _load(self) -> tuple[str | None, dict[str, list[CalendarEvent]]]:
        if not self._path.exists():
            return None, {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            events = {t: [CalendarEvent(**e) for e in evs] for t, evs in raw["events"].items()}
            return raw["day"], events
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("earnings calendar cache unreadable, starting empty: %s", e)  # refetched on first request
            return None, {}

    def _save(self) -> None:
        _write_json(self._path, {"day": self._day, "events": {
            t: [{"date": e.date, "slot": e.slot, "period": e.period} for e in evs] for t, evs in self._events.items()
        }})


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
        self._write(self.keys() - {(ticker, day)})

    def _write(self, keys: set[tuple[str, str]]) -> None:
        # A key past the released window can no longer match a listed row.
        oldest = (datetime.fromtimestamp(self._wall(), BJT).date() - timedelta(days=RELEASED_DAYS + 1)).isoformat()
        _write_json(self._path, sorted([t, d] for t, d in keys if d >= oldest))


class Earnings:
    """The `GET /api/earnings` body: judged rows with research and ignore flags."""

    def __init__(self, calendar: CalendarCache, ignored: IgnoreStore, wall: Callable[[], float] = time.time) -> None:
        self.calendar, self.ignored, self._wall = calendar, ignored, wall

    def snapshot(self, pool: list[str], chat_starts: list[tuple[str, float]]) -> dict[str, Any]:
        events, day, error = self.calendar.get(pool)
        now = datetime.fromtimestamp(self._wall(), BJT)
        ignored = self.ignored.keys()
        rows = [r for t, evs in events.items() if (r := judge(t, evs, now)) is not None]
        released = sorted((r for r in rows if r.status == "released"), key=lambda r: r.release_at, reverse=True)
        upcoming = sorted((r for r in rows if r.status == "upcoming"), key=lambda r: r.release_at)
        return {"fetched_day": day, "error": error,
                "items": [_item(r, ignored, chat_starts) for r in released + upcoming]}


def _item(r: Row, ignored: set[tuple[str, str]], chat_starts: list[tuple[str, float]]) -> dict[str, Any]:
    moment = r.release_at.timestamp()
    return {
        "ticker": r.ticker, "status": r.status, "date": r.event.date, "day_offset": r.day_offset,
        "slot": r.event.slot, "release_at": r.release_at.isoformat(), "period": r.event.period,
        # Any 讨论 chat on the ticker started after the release counts, wherever it was opened from.
        "researched": any(t == r.ticker and created >= moment for t, created in chat_starts),
        "ignored": (r.ticker, r.event.date) in ignored,
    }
