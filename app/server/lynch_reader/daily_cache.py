"""Per-ticker longbridge data fetched in full on a daily schedule, persisted across restarts.

The earnings calendar (earnings.py) and the anomaly baselines (anomalies.py) share this policy. Tickers fall in
groups (the calendar: one; the baselines: one per market), and a group is fetched in full once its latest
scheduled run has passed since its last full fetch. Between full fetches, tickers never seen (a new framework) are
fetched alone and tickers that left the pool are only filtered. A failure keeps the old data and is not retried
for RETRY_S. When every ticker of a group failed (logged out, CLI missing) its run stays open for a full refetch;
when only some did (a renamed symbol), the run is done and only those tickers are retried, so one bad symbol does
not refetch the whole group every RETRY_S.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar

from lynch_reader.market_hours import MARKET_TZ

log = logging.getLogger(__name__)

T = TypeVar("T")

RETRY_S = 30 * 60  # after a failed fetch; the home page asks every 5 minutes
BJT = MARKET_TZ["A"]  # the user's day


@dataclass
class TickerFetch(Generic[T]):
    data: dict[str, T] = field(default_factory=dict)  # every ticker that answered
    failed: list[str] = field(default_factory=list)  # tickers that did not
    error: str | None = None  # the first failure, for the page's banner


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # read on every request: never leave it torn


class DailyTickerCache(Generic[T]):
    """See the module docstring.

    Two locks: `_fetch_lock` makes concurrent refreshing callers wait for one fetch, which can take a minute;
    `_lock` guards the state only briefly, so a read (`refresh=False`) never waits on a fetch in flight.
    """

    def __init__(self, path: Path, wall: Callable[[], float] = time.time) -> None:
        self._path, self._wall = path, wall
        self._fetch_lock = threading.Lock()
        self._lock = threading.Lock()
        self._fetched, self._data = self._load()  # group -> time of its last full fetch
        self._failed: set[str] = set()  # tickers to retry alone, once the back-off has passed
        self._failed_at: float | None = None
        self._error: str | None = None

    def get(self, pool: list[str], *, refresh: bool = True) -> tuple[dict[str, T], str | None]:
        """(data of the pool tickers it holds, last error). `refresh=False` only reads."""
        if refresh:
            with self._fetch_lock:
                self._maybe_refresh(pool)
        with self._lock:
            return {t: self._data[t] for t in pool if t in self._data}, self._error

    def _last_full_fetch(self, group: str) -> float | None:
        with self._lock:
            return self._fetched.get(group)

    def _maybe_refresh(self, pool: list[str]) -> None:
        now = self._wall()
        if self._failed_at is not None and now - self._failed_at < RETRY_S:
            return
        due = {g for g in map(self._group, pool) if self._fetched.get(g, -math.inf) < self._run_at(g, now)}
        tickers = [t for t in pool if self._group(t) in due or t not in self._data or t in self._failed]
        if tickers:
            self._refresh(tickers, due, now)

    def _refresh(self, tickers: list[str], due: set[str], now: float) -> None:
        # The fetch runs outside `_lock`; only `_fetch_lock` (held by the caller) keeps it single.
        try:
            got = self._fetch(tickers, datetime.fromtimestamp(now, BJT).date())
        except Exception as e:  # noqa: BLE001 — the fetcher reports CLI failures itself; this is the backstop
            log.warning("%s fetch raised: %s", type(self).__name__, e)
            got = TickerFetch({}, list(tickers), str(e))
        with self._lock:
            self._data.update({t: self._merge(t, self._data.get(t), v, now) for t, v in got.data.items()})
            self._failed = (self._failed - set(tickers)) | set(got.failed)
            if got.failed:
                self._failed_at = now
                self._error = f"{got.error}（{len(self._failed)} 只票未更新）"
            else:
                self._failed_at = None
                self._error = None if not self._failed else self._error
            answered = {self._group(t) for t in got.data}
            for g in due & answered:  # someone in the group answered: not an outage
                self._fetched[g] = now
            self._save()

    def _load(self) -> tuple[dict[str, float], dict[str, T]]:
        if not self._path.exists():
            return {}, {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return dict(raw["fetched"]), {t: self._decode(v) for t, v in raw["data"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("%s unreadable, starting empty: %s", self._path.name, e)  # refetched on the next refresh
            return {}, {}

    def _save(self) -> None:
        write_json(self._path, {"fetched": self._fetched, "data": {t: self._encode(v) for t, v in self._data.items()}})

    def _group(self, ticker: str) -> str:
        """The group whose schedule refetches `ticker`."""
        raise NotImplementedError

    def _run_at(self, group: str, now: float) -> float:
        """When `group`'s latest scheduled full fetch at or before `now` fell due."""
        raise NotImplementedError

    def _fetch(self, tickers: list[str], today: date) -> TickerFetch[T]:
        raise NotImplementedError

    def _merge(self, ticker: str, old: T | None, new: T, now: float) -> T:
        """What to hold for `ticker` once `new` answered over `old`: the answer, unless the source loses data."""
        return new

    def _encode(self, value: T) -> Any:
        raise NotImplementedError

    def _decode(self, raw: Any) -> T:
        raise NotImplementedError
