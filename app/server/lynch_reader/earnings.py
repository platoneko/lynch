"""Earnings list for the app: which pool tickers just released or are about to release (AGENTS.md「app 对话」).

The calendar comes from longbridge (eval/lib/earnings_lb.py). A release counts as out once its session slot's
moment has passed on the market's own clock: before-open releases at the open, everything else at the close.
Nothing waits for report data to reach longbridge.

Once results are in, longbridge rewrites the entry (seen 2026-10): a US after-close release moves to the next day
(Beijing's), extra entries appear (year-to-date, prior year), and some releases drop out altogether. The earnings
call's start survives the rewrite, so a reported entry counts from it when it is earlier, and a release already out
stays cached when the calendar drops it.

A release counts as handled once the framework was revised after it (`updated_since`); the home list and the doc
page flag a framework still unrevised STALE_DAYS into its last release.
"""
from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping
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
STALE_DAYS = 30  # an unrevised framework is flagged through its last release's 30th local day (用户 2026-10-30)
# Entries this close are one release (a quarter and its half-year report listed a day apart); releases further
# apart are separate, so an A-share Q1 report days after the annual one needs its own revision.
PAIR_DAYS = 2

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
Revised = Callable[[str, datetime], bool]  # (ticker, release start) -> was its framework revised after it


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
    tz = MARKET_TZ[market]
    moment = datetime.combine(date.fromisoformat(ev.date), OPEN if ev.slot == "before" else CLOSE[market], tz)
    if ev.reported and ev.call_at is not None:  # a rewritten entry's date may be a day late; its call is not
        moment = min(moment, datetime.fromtimestamp(ev.call_at, tz))
    return moment


def judge(ticker: str, events: list[CalendarEvent], now: datetime) -> Row | None:
    """The ticker's one row: its latest release inside the released window, else its nearest upcoming one."""
    rows = _rows(ticker, events, now)
    released = [r for r in rows if r.status == "released" and -r.day_offset <= RELEASED_DAYS]
    if released:
        return _one_release(released)
    upcoming = [r for r in rows if r.status == "upcoming" and r.day_offset <= UPCOMING_DAYS]
    return min(upcoming, key=lambda r: r.release_at) if upcoming else None


def last_release(ticker: str, events: list[CalendarEvent], now: datetime) -> Row | None:
    """The latest release out within STALE_DAYS, its entries of the PAIR_DAYS before it taken as the same one."""
    released = [r for r in _rows(ticker, events, now) if r.status == "released" and -r.day_offset <= STALE_DAYS]
    if not released:
        return None
    latest = max(r.release_at for r in released)
    return _one_release([r for r in released if latest - r.release_at <= timedelta(days=PAIR_DAYS)])


def next_release(ticker: str, events: list[CalendarEvent], now: datetime) -> Row | None:
    upcoming = [r for r in _rows(ticker, events, now) if r.status == "upcoming"]
    return min(upcoming, key=lambda r: r.release_at) if upcoming else None


def _rows(ticker: str, events: list[CalendarEvent], now: datetime) -> list[Row]:
    """Every event on its market's clock; none for a market without one."""
    # Results add entries sharing the release's call, a 前三季度 one dated a day early: the latest-dated stands for
    # it. Only entries with results: before them a scheduled call can be shared by a quarter and its half-year.
    by_call: dict[int, str] = {}
    for ev in events:
        if ev.reported and ev.call_at is not None:
            by_call[ev.call_at] = max(by_call.get(ev.call_at, ev.date), ev.date)
    rows: list[Row] = []
    for ev in events:
        if ev.reported and ev.call_at is not None and ev.date != by_call[ev.call_at]:
            continue
        moment = release_at(ticker, ev)
        if moment is None:
            return []
        offset = (moment.date() - now.astimezone(moment.tzinfo).date()).days
        day = (moment.date().isoformat(),)  # the corrected day: stable across the calendar's rewrite
        rows.append(Row(ticker, "released" if moment <= now else "upcoming", ev, moment, offset, moment, day))
    return rows


def _one_release(released: list[Row]) -> Row:
    latest = max(released, key=lambda r: r.release_at)
    return replace(latest, since=min(r.release_at for r in released),
                   dates=tuple(sorted({d for r in released for d in r.dates})))


def updated_since(updated: date | None, since: datetime, changed_at: Callable[[], float | None]) -> bool:
    """Whether the framework was revised after a release starting at `since`, by its header's 更新 day.

    The day is compared on Beijing's calendar. On the release's own Beijing day the header cannot tell an edit made
    that morning from one made after the release (HK / A-share results come out at the close), so the file's last
    change decides.
    """
    if updated is None:
        return False
    day = since.astimezone(BJT).date()
    if updated != day:
        return updated > day
    changed = changed_at()
    return changed is not None and changed > since.timestamp()


def file_changed_at(path: Path) -> float | None:
    """When a framework file last changed: its uncommitted edit's mtime, else its last commit; None if unknown."""
    def git(*args: str) -> str | None:
        try:
            # No optional locks: `status` must not take index.lock while a chat commits in frameworks/.
            proc = subprocess.run(["git", "--no-optional-locks", "-C", str(path.parent), *args, "--", path.name],
                                  capture_output=True, text=True, timeout=2, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    dirty = git("status", "--porcelain")
    if dirty:  # a landing not committed yet
        try:
            return path.stat().st_mtime
        except OSError:
            return None
    last = git("log", "-1", "--format=%at")  # author time: a rebase rewrites the committer's
    return float(last) if last else None


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

    def _merge(self, ticker: str, old: list[CalendarEvent] | None, new: list[CalendarEvent],
               now: float) -> list[CalendarEvent]:
        t = datetime.fromtimestamp(now, BJT)

        def out(events: list[CalendarEvent]) -> list[CalendarEvent]:
            return [e for e in events if (m := release_at(ticker, e)) is not None and m <= t]

        if old is None:
            return new
        # Kept through the stale flag's window; one extra day back, as in `_fetch`.
        kept_from = (t.date() - timedelta(days=STALE_DAYS + 1)).isoformat()
        fetched_from = (t.date() - timedelta(days=RELEASED_DAYS + 1)).isoformat()
        cached = [e for e in out(old) if e.date >= kept_from]
        # Before the fetch window the answer cannot speak for a release. Inside it, a cached release is kept only
        # when the answer holds none already out: a rewritten one, counted from its call, replaces them.
        older = [e for e in cached if e.date < fetched_from and e not in new]
        if out(new):
            return older + new
        return older + [e for e in cached if e.date >= fetched_from] + new

    def _encode(self, value: list[CalendarEvent]) -> Any:
        return [{"date": e.date, "slot": e.slot, "period": e.period, "call_at": e.call_at, "reported": e.reported}
                for e in value]

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

    def snapshot(self, pool: list[str], revised: Revised,
                 tag: Callable[[str], dict[str, Any] | None] = lambda t: None) -> dict[str, Any]:
        events, day, error = self.calendar.get(pool)
        now = datetime.fromtimestamp(self._wall(), BJT)
        ignored = self.ignored.keys()
        rows = [r for t, evs in events.items() if (r := judge(t, evs, now)) is not None]
        released = sorted((r for r in rows if r.status == "released"), key=lambda r: r.release_at, reverse=True)
        upcoming = sorted((r for r in rows if r.status == "upcoming"), key=lambda r: r.release_at)
        # Research is judged on the home list's release, so the page and the 财报后未更新 filter agree.
        handled = {r.ticker for r in released
                   if (last := last_release(r.ticker, events[r.ticker], now)) and revised(r.ticker, last.since)}
        return {"fetched_day": day, "error": error,
                "items": [_item(r, ignored, r.ticker in handled, tag(r.ticker)) for r in released + upcoming]}

    def ticker_dates(self, pool: list[str], revised: Revised) -> dict[str, dict[str, Any]]:
        """Each pool ticker's last (within STALE_DAYS) and next release, and whether its framework still lags the
        last one. Reads the cache only: the earnings page and the anomaly upkeep refresh it."""
        events, _, _ = self.calendar.get(pool, refresh=False)
        now = datetime.fromtimestamp(self._wall(), BJT)
        out = {}
        for t, evs in events.items():
            last, upcoming = last_release(t, evs, now), next_release(t, evs, now)
            out[t] = {"last_earnings": _when(last), "next_earnings": _when(upcoming),
                      "stale_after_earnings": last is not None and not revised(t, last.since)}
        return out


def revised_by(updated: Mapping[str, date | None], frameworks_dir: Path) -> Revised:
    """`Revised` from the frameworks' header days; pool tickers only, as their file is the ticker's name."""
    return lambda t, since: updated_since(updated.get(t), since, lambda: file_changed_at(frameworks_dir / f"{t}.md"))


def _when(r: Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {"date": r.release_at.date().isoformat(), "slot": r.event.slot, "period": r.event.period,
            "release_at": r.release_at.isoformat(), "day_offset": r.day_offset}


def _item(r: Row, ignored: set[tuple[str, str]], researched: bool, anomaly: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "ticker": r.ticker, "status": r.status, "date": r.release_at.date().isoformat(), "day_offset": r.day_offset,
        "slot": r.event.slot, "release_at": r.release_at.isoformat(), "period": r.event.period,
        "researched": researched,
        "ignored": any((r.ticker, d) in ignored for d in r.dates),
        "anomaly": anomaly,  # the ticker's anomaly filed under earnings (anomalies.py), if any
    }
