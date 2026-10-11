"""Anomaly alerts for the app: a pool ticker whose price moved far from its base close (AGENTS.md「app 对话」).

A price counts when its move from its own session's base close is MOVE_MULT× the ticker's usual daily move, or,
in the regular session, VOLUME_MOVE_MULT× on VOLUME_MULT× its usual volume. Prices come from the one quote copy
(catalog.QuoteCache), judged on each new batch; the usual move and volume come from daily bars fetched once per
Beijing day. Only the price at fetch time is judged: a spike that reverts between fetches is a stray print.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from lynch_reader.catalog import REPO_ROOT, QuoteBatch  # also puts the repo root on sys.path for eval.lib
from lynch_reader.daily_cache import BJT, DailyTickerCache, TickerFetch, write_json
from lynch_reader.earnings import CalendarCache, judge
from lynch_reader.market_hours import MARKET_TZ

from eval.lib.frameworks import market_bucket  # noqa: E402
from eval.lib.price import DailyBar, Quote, fetch_daily_bars  # noqa: E402

log = logging.getLogger(__name__)

LOOKBACK = 60  # sessions behind the usual move and volume
MOVE_MULT = 4.0
VOLUME_MOVE_MULT = 2.0
VOLUME_MULT = 5.0
KEEP_DAYS = 7  # listed for 7 days after it first fired; forgotten 7 days after its last hit
BAR_PAUSE_S = 0.2  # between kline calls: back-to-back calls trip Longbridge's per-second cap
DAY_S = 86400

ANOMALIES_DIR = REPO_ROOT / "output" / "anomalies"
EVENTS_FILE = ANOMALIES_DIR / "events.json"
BASELINE_FILE = ANOMALIES_DIR / "baseline.json"


@dataclass(frozen=True)
class Baseline:
    sigma: float  # population stdev of the last LOOKBACK daily close-to-close returns
    volume: float  # median volume of the same sessions


def baseline_from_bars(ticker: str, bars: list[DailyBar], now: datetime) -> Baseline | None:
    """From closed sessions only: the bar dated the market's local today may still be open."""
    tz = MARKET_TZ.get(market_bucket(ticker))
    if tz is None:
        return None
    today = now.astimezone(tz).date()
    closed = [b for b in bars if b.at.astimezone(tz).date() < today]
    if len(closed) < LOOKBACK + 1:
        return None
    window = closed[-(LOOKBACK + 1):]
    if any(b.close <= 0 for b in window):
        return None
    sigma = statistics.pstdev(b.close / a.close - 1 for a, b in zip(window, window[1:]))
    if sigma <= 0:
        return None
    return Baseline(sigma, statistics.median(b.volume for b in window[1:]))


class BaselineCache(DailyTickerCache[Baseline | None]):
    """Each ticker's usual move and volume, refreshed by the policy in daily_cache.py. A ticker fetched with too
    few bars holds None: judged never, refetched only on the next full fetch."""

    def __init__(self, path: Path, fetch: Callable[[str, int], list[DailyBar]] = fetch_daily_bars,
                 wall: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self._bars, self._sleep = fetch, sleep
        super().__init__(path, wall)

    def _fetch(self, tickers: list[str], today: date) -> TickerFetch[Baseline | None]:
        out: TickerFetch[Baseline | None] = TickerFetch()
        now = datetime.fromtimestamp(self._wall(), BJT)
        for i, t in enumerate(tickers):
            if i:
                self._sleep(BAR_PAUSE_S)
            try:
                bars = self._bars(t, LOOKBACK + 2)  # one may be today's, still open
            except Exception as e:  # noqa: BLE001 — external CLI: missing, logged out, rate-limited, bad JSON
                log.warning("daily bars failed %s: %s", t, e)
                out.failed.append(t)
                out.error = out.error or str(e)
                continue
            out.data[t] = baseline_from_bars(t, bars, now)
        return out

    def _encode(self, value: Baseline | None) -> Any:
        return None if value is None else asdict(value)

    def _decode(self, raw: Any) -> Baseline | None:
        return None if raw is None else Baseline(**raw)


@dataclass(frozen=True)
class Hit:
    move: float  # price / base close - 1
    mult: float  # |move| / the usual daily move
    session: str  # regular | pre | post | overnight
    at: float  # trade time for extended prices, fetch time for regular ones
    reason: str  # move | volume
    volume_mult: float | None  # today's volume / the usual, when the volume rule fired


def hits(q: Quote, base: Baseline, fetched_at: float) -> list[tuple[str, Hit]]:
    """(base close key, hit) for each of the quote's prices that moved far enough."""
    prices = [(q.last, q.prev_close, "regular", fetched_at, q.volume)]
    prices += [(x.last, x.prev_close, x.session, x.at.timestamp(), None) for x in q.extended]
    out = []
    for price, ref, session, at, volume in prices:
        if ref is None or ref <= 0 or not math.isfinite(price) or price <= 0:
            continue
        move = price / ref - 1
        mult = abs(move) / base.sigma
        volume_mult = volume / base.volume if volume is not None and base.volume > 0 else None
        if mult >= MOVE_MULT:
            out.append((repr(ref), Hit(move, mult, session, at, "move", None)))
        elif (session == "regular" and mult >= VOLUME_MOVE_MULT
              and volume_mult is not None and volume_mult >= VOLUME_MULT):
            out.append((repr(ref), Hit(move, mult, session, at, "volume", volume_mult)))
    return out


@dataclass(frozen=True)
class Event:
    ticker: str
    ref: str  # the base close as repr(float): one event per ticker and base close, so stale quotes fold in
    first_at: float
    first: Hit
    peak: Hit  # the largest |move| seen for this base close
    last_hit: float
    earnings: bool  # the ticker was on the earnings list when it first fired: shown there, never here
    ignored: bool = False


class AnomalyStore:
    """Every event, keyed by (ticker, base close). The server is the only writer; the quote listener and the
    ignore routes both write, from different threads, so a lock guards the file."""

    def __init__(self, path: Path, wall: Callable[[], float] = time.time) -> None:
        self._path, self._wall = path, wall
        self._lock = threading.Lock()
        self._events = self._load()

    def record(self, found: list[tuple[str, str, Hit]], in_earnings: Callable[[str], bool]) -> None:
        with self._lock:
            now = self._wall()
            for ticker, ref, hit in found:
                e = self._events.get((ticker, ref))
                if e is None:
                    self._events[(ticker, ref)] = Event(ticker, ref, now, hit, hit, now, in_earnings(ticker))
                else:
                    peak = hit if abs(hit.move) > abs(e.peak.move) else e.peak
                    self._events[(ticker, ref)] = replace(e, peak=peak, last_hit=now)
            cutoff = now - KEEP_DAYS * DAY_S
            self._events = {k: e for k, e in self._events.items() if e.last_hit >= cutoff}
            self._save()

    def set_ignored(self, ticker: str, ref: str, ignored: bool) -> None:
        with self._lock:
            e = self._events[(ticker, ref)]  # KeyError: no such event
            self._events[(ticker, ref)] = replace(e, ignored=ignored)
            self._save()

    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events.values())

    def _load(self) -> dict[tuple[str, str], Event]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            events = [Event(**{**d, "first": Hit(**d["first"]), "peak": Hit(**d["peak"])}) for d in raw]
            return {(e.ticker, e.ref): e for e in events}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("anomaly events unreadable, starting empty: %s", e)
            return {}

    def _save(self) -> None:
        write_json(self._path, [asdict(e) for e in self._events.values()])


class Anomalies:
    """The quote listener, the poller's upkeep, and the `GET /api/anomalies` body."""

    def __init__(self, store: AnomalyStore, baselines: BaselineCache, calendar: CalendarCache,
                 wall: Callable[[], float] = time.time) -> None:
        self._store, self._baselines, self._calendar, self._wall = store, baselines, calendar, wall

    @property
    def store(self) -> AnomalyStore:
        return self._store

    def observe(self, batch: QuoteBatch, pool: list[str]) -> None:
        """Judge a new batch. `pool` leaves archived tickers out. Reads caches only: never waits on longbridge."""
        bases, _, _ = self._baselines.get(pool, refresh=False)
        calendar, _, _ = self._calendar.get(pool, refresh=False)
        now = datetime.fromtimestamp(self._wall(), BJT)
        found = [(t, ref, hit) for t in pool
                 if (q := batch.quotes.get(t)) is not None and (base := bases.get(t)) is not None
                 for ref, hit in hits(q, base, batch.fetched_at)]
        self._store.record(found, lambda t: judge(t, calendar.get(t, []), now) is not None)

    def upkeep(self, pool: list[str]) -> None:
        """The poller's daily fetches, off the request path: baselines, and the calendar `observe` reads."""
        self._baselines.get(pool)
        self._calendar.get(pool)

    def snapshot(self, pool: list[str], chat_starts: list[tuple[str, float]],
                 quote_error: str | None) -> dict[str, Any]:
        since = self._wall() - KEEP_DAYS * DAY_S
        latest: dict[str, Event] = {}
        for e in self._store.events():
            if e.earnings or e.ticker not in pool or e.first_at < since:
                continue
            if e.ticker not in latest or e.first_at > latest[e.ticker].first_at:
                latest[e.ticker] = e
        _, _, baseline_error = self._baselines.get(pool, refresh=False)
        return {
            "error": quote_error or baseline_error,
            "items": [{**_hit_json(e),
                       # Any 讨论 chat on the ticker started after it fired counts, wherever it was opened from.
                       "researched": any(t == e.ticker and created >= e.first_at for t, created in chat_starts),
                       "ignored": e.ignored}
                      for e in sorted(latest.values(), key=lambda e: e.first_at, reverse=True)],
        }

    def earnings_tag(self, ticker: str) -> dict[str, Any] | None:
        """The ticker's newest anomaly filed under earnings within KEEP_DAYS, for its earnings row."""
        since = self._wall() - KEEP_DAYS * DAY_S
        filed = [e for e in self._store.events() if e.earnings and e.ticker == ticker and e.first_at >= since]
        return _hit_json(max(filed, key=lambda e: e.first_at)) if filed else None


def _hit_json(e: Event) -> dict[str, Any]:
    p = e.peak
    return {
        "ticker": e.ticker, "ref": e.ref, "first_at": _iso(e.first_at),
        "move": p.move, "mult": p.mult, "session": p.session, "at": _iso(p.at),
        "reason": p.reason, "volume_mult": p.volume_mult,
    }


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, BJT).isoformat(timespec="seconds")  # the page shows Beijing time
