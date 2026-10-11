"""Ticker rows for the reader: framework header fields plus zones on a live Longbridge quote."""
from __future__ import annotations

import logging
import math
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Set
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.axes import AxisMeta, Consensus, list_axis_paths, load_axis  # noqa: E402
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

__all__ = [
    "FRAMEWORKS_DIR", "REPO_ROOT", "Quote", "QuoteBatch", "QuoteCache", "QuotePoller", "QuoteSnapshot",
    "axis_path_in", "catalog", "framework_path", "quote_failure", "quote_pool",
]

log = logging.getLogger("lynch_reader")

ROW_KEYS = (
    "ticker", "name", "market", "lynch_primary", "lynch_secondary", "currency",
    "L", "M", "H", "option", "candidate", "axes", "ref_axes", "consensus", "band", "quote_error", "error", "archived",
)
# Only scenario axes sit on cards and filters; the rates axis mounts every USD ticker
# and gives sensitivity readings, never anchors (AGENTS.md「r_f 读数行」).
SCENARIO_AXIS = "shared_scenario_axis"

# The poller's pace; the home list and the anomaly checks follow the same 5 minutes (spec).
QUOTE_POLL_S = 300.0
QUOTE_TIMEOUT_S = 15
NO_QUOTE = "无报价（停牌或代码变更）"
WAITING_QUOTE = "等待下一次取价"
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
    fetched_at: float  # wall clock, seconds since the epoch: `as_of` and market-hours checks


@dataclass(frozen=True)
class QuoteSnapshot:
    batch: QuoteBatch | None  # the latest good batch; None until one has been fetched
    error: Exception | None  # the latest attempt's failure; None once one succeeds


class QuoteCache:
    """The one copy of the pool's quotes (AGENTS.md「app 对话」). Two things fetch: the poller (`poll`) and the
    first read before any attempt (`read`); everything else only reads.

    A failed attempt keeps the last good batch and reports its error beside it. The lock makes concurrent
    callers wait for one CLI call. Listeners hear every new batch on the fetching thread, under the lock.
    """

    def __init__(
        self,
        fetch: Callable[[list[str]], dict[str, Quote]] = partial(
            fetch_quotes, allow_missing=True, timeout_s=QUOTE_TIMEOUT_S
        ),
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._fetch = fetch
        self._wall = wall
        self._lock = threading.Lock()
        self._batch: QuoteBatch | None = None
        self._error: Exception | None = None
        self._tried = False
        self._listeners: list[Callable[[QuoteBatch], None]] = []

    def subscribe(self, listener: Callable[[QuoteBatch], None]) -> None:
        self._listeners.append(listener)

    def read(self, symbols: list[str]) -> QuoteSnapshot:
        with self._lock:
            if not self._tried:  # just started: the first reader waits for the first batch
                self._fetch_batch(symbols)
            return QuoteSnapshot(self._batch, self._error)

    def poll(self, symbols: list[str]) -> None:
        with self._lock:
            b = self._batch
            if b is None or set(symbols) - set(b.symbols) or self._traded_since(b, symbols):
                self._fetch_batch(symbols)

    def _traded_since(self, b: QuoteBatch, symbols: list[str]) -> bool:
        # Market hours are wall-clock hours.
        now = self._wall()
        return any(traded_between(m, b.fetched_at, now) for m in {market_bucket(s) for s in symbols})

    def _fetch_batch(self, symbols: list[str]) -> None:
        key = tuple(symbols)
        try:
            quotes = self._fetch(list(key))
        except Exception as e:  # noqa: BLE001 — external CLI: missing, non-zero exit, timeout, bad JSON
            log.warning("quote fetch failed: %s", e)
            self._error = e
            return
        finally:
            self._tried = True  # a failed first attempt counts: readers do not each retry the CLI
        self._error = None
        for s in key:
            if not _usable(quotes.get(s)):
                log.warning("no usable quote: %s: %s", s, quotes.get(s))
        self._batch = QuoteBatch(key, quotes, self._wall())
        for listener in self._listeners:
            try:
                listener(self._batch)
            except Exception:  # noqa: BLE001 — a listener (anomaly checks) must not cost the list its prices
                log.exception("quote listener failed")


class QuotePoller:
    """The background thread behind the one quote copy: a round at start, then every `interval_s` it polls the
    cache and runs `after` (the day's anomaly-baseline and calendar fetches). The app's lifespan starts and stops it.
    """

    def __init__(self, quotes: QuoteCache, symbols: Callable[[], list[str]],
                 after: Callable[[], None] = lambda: None, interval_s: float = QUOTE_POLL_S) -> None:
        self._quotes, self._symbols, self._after, self._interval_s = quotes, symbols, after, interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        # Each half failing alone is logged; neither ends the thread.
        try:
            self._quotes.poll(self._symbols())
        except Exception:  # noqa: BLE001
            log.exception("quote poll failed")
        try:
            self._after()
        except Exception:  # noqa: BLE001
            log.exception("poll upkeep failed")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="quote-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)  # a CLI call in flight is abandoned with the daemon thread

    def _loop(self) -> None:
        while True:
            self.run_once()
            if self._stop.wait(self._interval_s):
                return


def catalog(frameworks_dir: Path, quotes: QuoteCache, archived: Set[str] = frozenset()) -> dict[str, Any]:
    # Step 1: framework header fields; a bad file costs only its own row. Archived rows stay in the
    # list, quotes included: the web hides them unless asked to show them.
    parsed = [_ticker_row(p) for p in list_framework_paths(frameworks_dir)]
    for row, _ in parsed:
        row["archived"] = row["ticker"] in archived
    priced = [(row, meta) for row, meta in parsed if meta is not None]
    axes = _scenario_axes(frameworks_dir)
    for row, meta in priced:
        row["axes"] = [a.name for a in axes if meta.ticker in a.mounts]
        row["ref_axes"] = [a.name for a in axes if meta.ticker in a.refs]

    # Step 2: the one quote batch, read. A failed attempt keeps the last good batch, each row showing its own
    # fetch time; with no batch at all every band stays empty.
    snap = quotes.read(quote_pool(frameworks_dir))
    quote_error = None if snap.error is None else quote_failure(snap.error, snap.batch)
    if snap.batch is not None:
        us_close = us_regular_close(snap.batch.fetched_at)
        for row, meta in priced:
            _price_row(row, meta, snap.batch, us_close)

    return {
        "zones": [{"id": z, "label": ZONE_CN[z]} for z in ZONE_ORDER],
        "lynch_classes": [{"id": k, "label": v} for k, v in LYNCH_CN.items()],
        "quote_error": quote_error,
        "axes": [_axis_json(a) for a in axes],
        "tickers": [row for row, _ in parsed],
    }


def quote_pool(frameworks_dir: Path) -> list[str]:
    """Every framework, archived ones included (展示归档 shows their prices): the one symbol list quotes cover."""
    return [p.stem for p in list_framework_paths(frameworks_dir)]


def framework_path(ticker: str, frameworks_dir: Path) -> Path | None:
    """Resolve only pool members; the requested name is never joined into a path."""
    for p in list_framework_paths(frameworks_dir):
        if p.stem == ticker:
            return p
    return None


def axis_path_in(name: str, frameworks_dir: Path) -> Path | None:
    """Resolve only existing axis files (the rates axis included); the name is never joined into a path."""
    for p in list_axis_paths(frameworks_dir / "axes"):
        if p.stem == name:
            return p
    return None


def _scenario_axes(frameworks_dir: Path) -> list[AxisMeta]:
    """Scenario axes by file name; a bad axis file costs only itself."""
    out: list[AxisMeta] = []
    for p in list_axis_paths(frameworks_dir / "axes"):
        try:
            axis = load_axis(p)
        except Exception as e:  # noqa: BLE001 — one bad axis must not blank the list
            log.warning("axis parse failed: %s: %s", p.name, e)
            continue
        if axis.kind == SCENARIO_AXIS:
            out.append(axis)
    return out


def _axis_json(a: AxisMeta) -> dict[str, Any]:
    return {"name": a.name, "label": a.label, "title": a.title,
            "consensus": _consensus_json(a.consensus), "tickers": list(a.mounts), "refs": list(a.refs)}


def _consensus_json(c: Consensus | None) -> dict[str, str] | None:
    return None if c is None else {"tier": c.tier, "note": c.note}


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
        axes=[],
        consensus=_consensus_json(meta.consensus),
    )
    return row, meta


def _price_row(row: dict[str, Any], meta: FrameworkMeta, batch: QuoteBatch, us_close: datetime) -> None:
    """The row's band: live price, fetch time, and zones by the daily digest's own rules."""
    if meta.ticker not in batch.symbols:  # a framework added since the last fetch
        row["quote_error"] = WAITING_QUOTE
        return
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
    """The newest price by trade time across the regular session, pre-market, post-market and overnight.

    Longbridge gives the regular price no trade time, so it is dated at `us_close`, the
    latest regular close (a session still running closes in the future: the regular price
    wins). Non-US rows carry no extended prices. The daily digest keeps the regular price;
    only the reader follows extended hours.
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


def quote_failure(e: Exception, batch: QuoteBatch | None) -> str:
    """One short banner line; a timeout's own message carries the whole symbol list."""
    if isinstance(e, subprocess.TimeoutExpired):
        text = f"取价超时（{QUOTE_TIMEOUT_S} 秒）"
    else:
        msg = str(e).strip()
        text = f"取价失败：{(msg.splitlines()[0] if msg else type(e).__name__)[:120]}"
    if batch is None:
        return text
    return f"{text}（显示的是 {datetime.fromtimestamp(batch.fetched_at, TZ):%H:%M} 的现价）"


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
