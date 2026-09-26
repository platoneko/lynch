"""Ticker rows for the reader: framework header fields plus zones on a live Longbridge quote."""
from __future__ import annotations

import logging
import math
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.frameworks import (  # noqa: E402
    FRAMEWORKS_DIR,
    FrameworkMeta,
    LYNCH_CN,
    list_framework_paths,
    load_framework,
    market_bucket,
)
from eval.lib.price import Quote, fetch_quotes  # noqa: E402
from eval.lib.zones import ZONE_CN, ZONE_ORDER, credited_option_band, price_zones  # noqa: E402
from lynch_reader.market_hours import traded_between, us_regular_close  # noqa: E402

__all__ = ["FRAMEWORKS_DIR", "REPO_ROOT", "Quote", "QuoteCache", "catalog", "framework_path"]

log = logging.getLogger("lynch_reader")

ROW_KEYS = (
    "ticker", "name", "market", "lynch_primary", "lynch_secondary", "currency",
    "L", "M", "H", "option", "candidate", "band", "quote_error", "error",
)

# Quotes are reused up to this age: it bounds Longbridge CLI calls. Past it they are
# still reused while no market in the pool has traded since (market_hours.py).
QUOTE_MAX_AGE_S = 300.0
# The refresh button is mainly for re-reading frameworks; its quotes need only be this fresh.
QUOTE_REFRESH_MAX_AGE_S = 60.0
# During an outage each call can take the whole timeout; requests in this window share one failure.
QUOTE_BACKOFF_S = 5.0
QUOTE_TIMEOUT_S = 15
NO_QUOTE = "无报价（停牌或代码变更）"
# Same timestamp form as the daily digest's `_as_of_iso` (scripts/daily_bands.py).
TZ = ZoneInfo("Asia/Shanghai")

_ASSUMPTIONS = re.compile(r"^## 2\. Assumptions[^\n]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
_CELL_SEP = re.compile(r"(?<!\\)\|")  # `\|` inside a cell is text, not a separator
# Assumption ids as the prose cites them: `A12`, `A23a`, `A2e-7c-4`, `A3′`.
_ASSUMPTION_ID = re.compile(r"(?<![0-9A-Za-z])A[0-9][0-9A-Za-z]*(?:-[0-9A-Za-z]+)*′?")


@dataclass(frozen=True)
class QuoteBatch:
    symbols: tuple[str, ...]
    quotes: dict[str, Quote]
    fetched_at: float  # wall clock, seconds since the epoch: only for `as_of`
    fetched_mono: float  # monotonic clock: for the max age


@dataclass(frozen=True)
class QuoteFailure:
    symbols: tuple[str, ...]
    error: Exception
    failed_mono: float


class QuoteCache:
    """One batched quote call for the whole pool, reused while younger than the caller's max age
    or while no market in the pool has traded since.

    The lock makes concurrent requests wait for one CLI call instead of each
    starting their own; those that waited get that call's batch or failure.
    A failure is reused only for QUOTE_BACKOFF_S, and never replaced by an older batch.
    """

    def __init__(
        self,
        fetch: Callable[[list[str]], dict[str, Quote]] = partial(
            fetch_quotes, allow_missing=True, timeout_s=QUOTE_TIMEOUT_S
        ),
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._fetch = fetch
        self._monotonic = monotonic
        self._wall = wall
        self._lock = threading.Lock()
        self._batch: QuoteBatch | None = None
        self._failure: QuoteFailure | None = None

    def get(self, symbols: list[str], max_age_s: float) -> QuoteBatch:
        key = tuple(symbols)
        with self._lock:
            # A pool that changed since (framework added or renamed) needs its own call.
            f = self._failure
            if f is not None and f.symbols == key and self._monotonic() - f.failed_mono < QUOTE_BACKOFF_S:
                raise f.error
            b = self._batch
            if b is None or b.symbols != key or not self._current(b, max_age_s):
                b = self._fetch_batch(key)
            return b

    def _current(self, b: QuoteBatch, max_age_s: float) -> bool:
        if self._monotonic() - b.fetched_mono < max_age_s:
            return True
        # Market hours are wall-clock hours; the age above stays monotonic.
        now = self._wall()
        return not any(traded_between(m, b.fetched_at, now) for m in {market_bucket(s) for s in b.symbols})

    def _fetch_batch(self, key: tuple[str, ...]) -> QuoteBatch:
        try:
            quotes = self._fetch(list(key))
        except Exception as e:
            # Stamped after the call returns, so requests queued behind it land inside the back-off.
            self._failure = QuoteFailure(key, e, self._monotonic())
            raise
        self._failure = None
        for s in key:
            if not _usable(quotes.get(s)):
                log.warning("no usable quote: %s: %s", s, quotes.get(s))
        self._batch = QuoteBatch(key, quotes, self._wall(), self._monotonic())
        return self._batch


def catalog(frameworks_dir: Path, quotes: QuoteCache, refresh: bool = False) -> dict[str, Any]:
    # Step 1: framework header fields; a bad file costs only its own row.
    parsed = [_ticker_row(p) for p in list_framework_paths(frameworks_dir)]
    priced = [(row, meta) for row, meta in parsed if meta is not None]

    # Step 2: one quote batch for every parsed row. When the whole call fails the list
    # still loads with every band empty; an old snapshot is never shown in its place.
    quote_error = None
    max_age_s = QUOTE_REFRESH_MAX_AGE_S if refresh else QUOTE_MAX_AGE_S
    try:
        batch = quotes.get([meta.ticker for _, meta in priced], max_age_s)
    except Exception as e:  # noqa: BLE001 — external CLI: missing, non-zero exit, timeout, bad JSON
        log.warning("quote fetch failed: %s", e)
        quote_error = _quote_failure(e)
    else:
        us_close = us_regular_close(batch.fetched_at)
        for row, meta in priced:
            _price_row(row, meta, batch, us_close)

    return {
        "zones": [{"id": z, "label": ZONE_CN[z]} for z in ZONE_ORDER],
        "lynch_classes": [{"id": k, "label": v} for k, v in LYNCH_CN.items()],
        "quote_error": quote_error,
        "tickers": [row for row, _ in parsed],
    }


def framework_path(ticker: str, frameworks_dir: Path) -> Path | None:
    """Resolve only pool members; the requested name is never joined into a path."""
    for p in list_framework_paths(frameworks_dir):
        if p.stem == ticker:
            return p
    return None


def _ticker_row(path: Path) -> tuple[dict[str, Any], FrameworkMeta | None]:
    row: dict[str, Any] = dict.fromkeys(ROW_KEYS)
    row["ticker"] = path.stem
    try:
        meta = load_framework(path)
        md = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — one bad file must not blank the whole list
        log.warning("framework parse failed: %s: %s", path.name, e)
        row["error"] = str(e)
        return row, None
    assumptions = _assumption_rows(md)
    row.update(
        name=meta.name,
        market=market_bucket(meta.ticker),
        lynch_primary=meta.lynch_primary,
        lynch_secondary=meta.lynch_secondary,
        currency=meta.currency,
        L=meta.L,
        M=meta.M,
        H=meta.H,
        option=_option(meta, assumptions),
        candidate=_candidate(meta, assumptions),
    )
    return row, meta


def _price_row(row: dict[str, Any], meta: FrameworkMeta, batch: QuoteBatch, us_close: datetime) -> None:
    """The row's band: live price, fetch time, and zones by the daily digest's own rules."""
    q = batch.quotes.get(meta.ticker)
    if not _usable(q):  # logged once when the batch was fetched
        row["quote_error"] = NO_QUOTE
        return
    price, session = _latest_price(q, us_close)
    try:
        zone, option_zone, candidate_zone = price_zones(price, meta)
    except ValueError as e:  # a +V band out of order is not caught at parse time
        log.warning("zones failed: %s: %s", meta.ticker, e)
        row["error"] = str(e)
        return
    row["band"] = {
        "price": price,
        "session": session,
        "as_of": datetime.fromtimestamp(batch.fetched_at, TZ).isoformat(timespec="seconds"),
        "zone": zone,
        "option_zone": option_zone,
        "candidate_zone": candidate_zone,
    }


def _latest_price(q: Quote, us_close: datetime) -> tuple[float, str | None]:
    """The regular price, or the newest US extended-hours price printed after the latest regular close.

    The daily digest keeps the regular price; only the reader follows extended hours.
    """
    later = [x for x in q.extended if x.at > us_close and _is_trade_price(x.last)]
    if not later:
        return q.last, None
    newest = max(later, key=lambda x: x.at)
    return newest.last, newest.session


def _usable(q: Quote | None) -> bool:
    return q is not None and _is_trade_price(q.last)


def _is_trade_price(p: float) -> bool:
    # NaN / inf cannot be serialized, and a price ≤ 0 is no trade price.
    return math.isfinite(p) and p > 0


def _quote_failure(e: Exception) -> str:
    """One short banner line; a timeout's own message carries the whole symbol list."""
    if isinstance(e, subprocess.TimeoutExpired):
        return f"取价超时（{QUOTE_TIMEOUT_S} 秒）"
    text = str(e).strip()
    first = text.splitlines()[0] if text else type(e).__name__
    return f"取价失败：{first[:120]}"


def _option(meta: FrameworkMeta, assumptions: dict[str, str]) -> dict[str, Any] | None:
    """The +V band plus where the option comes from: its Outputs line and the rows it cites."""
    band = credited_option_band(meta)
    if band is None:
        return None
    line = meta.option_line
    cited = dict.fromkeys(_ASSUMPTION_ID.findall(line)) if line else {}
    L, M, H = band
    return {
        "v_opt": meta.v_opt, "L": L, "M": M, "H": H, "line": line,
        "rows": [{"id": a, "text": assumptions[a]} for a in cited if a in assumptions],
    }


def _candidate(meta: FrameworkMeta, assumptions: dict[str, str]) -> dict[str, Any] | None:
    """The candidate band plus why it is only a candidate: its gate assumption's text."""
    if meta.candidate is None:
        return None
    L, M, H = meta.candidate
    gate = meta.candidate_gate
    return {
        "label": meta.candidate_label, "L": L, "M": M, "H": H,
        "gate": gate, "background": assumptions.get(gate) if gate else None,
    }


def _assumption_rows(md: str) -> dict[str, str]:
    """{first cell: second cell} of the §2 Assumptions table; the first row of an id wins."""
    section = _ASSUMPTIONS.search(md)
    if section is None:
        return {}
    rows: dict[str, str] = {}
    for line in section.group(1).splitlines():
        cells = [c.strip() for c in _CELL_SEP.split(line.strip())]
        # `| id | text | …` splits into ['', 'id', 'text', …].
        if len(cells) >= 3 and cells[0] == "":
            rows.setdefault(cells[1], cells[2])
    return rows
