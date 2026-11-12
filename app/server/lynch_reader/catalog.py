"""Ticker rows for the reader: framework header fields plus zones on a live Longbridge quote."""
from __future__ import annotations

import logging
import math
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.axes import list_axis_paths  # noqa: E402
from eval.lib.frameworks import (  # noqa: E402
    FRAMEWORKS_DIR,
    FrameworkIdentity,
    LYNCH_CN,
    list_framework_paths,
    market_bucket,
    parse_framework_identity,
)
from eval.lib.price import Quote, fetch_quotes  # noqa: E402
from eval.lib.axis_projection import collect_axes, project_axes  # noqa: E402
from eval.lib.zones import ZONE_CN, ZONE_ORDER, zone_of  # noqa: E402
from lynch_reader.market_hours import in_opening_auction, traded_between, us_regular_close  # noqa: E402
from lynch_reader.valuation_catalog import CatalogValuation, ValuationProvider, map_result  # noqa: E402

__all__ = [
    "FRAMEWORKS_DIR", "REPO_ROOT", "Quote", "QuoteBatch", "QuoteCache", "QuotePoller", "QuoteSnapshot",
    "axis_path_in", "catalog", "framework_path", "framework_updates", "quote_failure", "quote_pool",
]

log = logging.getLogger("lynch_reader")

ROW_KEYS = (
    "ticker", "name", "market", "lynch_primary", "lynch_secondary", "currency",
    "L", "M", "H", "option", "candidate", "axes", "ref_axes", "consensus", "band", "quote_error", "error", "archived",
    "framework_updated", "last_earnings", "next_earnings", "stale_after_earnings", "valuation_status", "review_flags",
    "option_review_status", "historical_evidence_flags", "historical_evidence_states", "fund_reference_status",
    "historical_evidence_differences", "historical_evidence_source", "historical_review_flags",
    "historical_option_status", "historical_fund_reference_status", "candidate_review_status", "historical_reference_bands",
    "historical_reference_only", "historical_reference_double_counting", "valuation_as_of",
)
# Earnings fields for each ticker, given header dates and their corresponding file versions.
FileVersion = tuple[int, int, int, int, int]
EarningsDates = Callable[[Mapping[str, date | None], Mapping[str, FileVersion | None]], Mapping[str, dict[str, Any]]]
# The poller's pace; the home list and the anomaly checks follow the same 5 minutes (spec).
QUOTE_POLL_S = 300.0
QUOTE_TIMEOUT_S = 15
NO_QUOTE = "无报价（停牌或代码变更）"
WAITING_QUOTE = "等待下一次取价"
# Same timestamp form as the daily digest's `_as_of_iso` (scripts/daily_bands.py).
TZ = ZoneInfo("Asia/Shanghai")

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
    """The one copy of the pool's quotes (AGENTS.md「app 对话」). Two things fetch: the poller (`poll`) and a read
    asking for symbols no attempt has asked for yet (`read`: the first read, or a framework added since); everything
    else only reads.

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
        self._asked: frozenset[str] | None = None  # the latest attempt's symbols; None before any attempt
        self._listeners: list[Callable[[QuoteBatch], None]] = []

    def subscribe(self, listener: Callable[[QuoteBatch], None]) -> None:
        self._listeners.append(listener)

    def read(self, symbols: list[str]) -> QuoteSnapshot:
        with self._lock:
            # Just started, or a new framework: the reader waits for the poll's fetch, taken early. A failed
            # attempt counts as asked, so reads never retry the CLI; the poll does.
            if self._asked is None or not self._asked.issuperset(symbols):
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
            self._asked = frozenset(key)
        self._error = None
        now = self._wall()
        quotes = {s: q for s, q0 in quotes.items() if (q := _traded_quote(s, q0, now)) is not None}
        for s in key:
            if not _usable(quotes.get(s)):
                log.warning("no usable quote: %s: %s", s, quotes.get(s))
        self._batch = QuoteBatch(key, quotes, now)
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


class _ParsedFiles:
    """Each file's parse, kept until its identity, size, mtime or ctime changes.

    Filesystem timestamps tick coarsely, so a same-size rewrite inside one tick keeps both; like git's racy-clean
    rule, a file modified within `SETTLE_S` of being read is never kept and is parsed again on every read.
    """

    SETTLE_S = 2.0

    def __init__(self, parse: Callable[[Path], Any]) -> None:
        self._parse = parse
        self._lock = threading.Lock()
        self._kept: dict[Path, tuple[FileVersion, Any]] = {}

    def __call__(self, paths: list[Path]) -> list[Any]:
        with self._lock:
            out, kept = [], {}
            for p in paths:
                try:
                    st = p.stat()
                except OSError:  # gone since listing: its parse reports the error
                    out.append(self._parse(p))
                    continue
                key = _stat_version(st)
                hit = self._kept.get(p)
                value = hit[1] if hit is not None and hit[0] == key else self._parse(p)
                if time.time() - st.st_mtime > self.SETTLE_S:
                    kept[p] = (key, value)
                out.append(value)
            self._kept = kept  # files no longer listed drop out
            return out


def catalog(frameworks_dir: Path, quotes: QuoteCache, archived: Set[str] = frozenset(),
            earnings_dates: EarningsDates | None = None, *,
            valuation_provider: ValuationProvider | None = None) -> dict[str, Any]:
    # Step 1: framework header fields; a bad file costs only its own row. Archived rows stay in the
    # list, quotes included: the web hides them unless asked to show them. Kept parses are shared, so each
    # read fills its own copy of the row.
    snapshots = _framework_rows(list_framework_paths(frameworks_dir))
    parsed = [(dict(row), identity) for (row, identity), _ in snapshots]
    valuations: dict[str, CatalogValuation] = {}
    projections = {}
    for row, identity in parsed:
        row["archived"] = row["ticker"] in archived
        if identity is None:
            continue
        try:
            record = valuation_provider.current(identity.ticker) if valuation_provider is not None else {"status": "missing"}
            status, valuation = map_result(identity.ticker, record)
        except Exception:  # noqa: BLE001 — failure to read one confirmed result must not take down the list
            log.exception("valuation provider failed: %s", identity.ticker)
            status, valuation = "unavailable", None
        row["valuation_status"] = status
        if valuation is None:
            projections[row['ticker']] = {'status': 'unknown', 'axes': []}
            continue
        projections[row['ticker']] = project_axes(row['ticker'], record)
        row.update(currency=valuation.currency, L=valuation.L, M=valuation.M, H=valuation.H,
                   valuation_as_of=valuation.valuation_as_of,
                   review_flags=list(valuation.review_flags), option_review_status=valuation.option_status,
                   historical_evidence_flags=list(valuation.historical_evidence_flags),
                   historical_evidence_states=valuation.historical_evidence_states,
                   fund_reference_status=valuation.fund_reference_status,
                   historical_evidence_differences=list(valuation.historical_evidence_differences),
                   historical_evidence_source=valuation.historical_evidence_source,
                   historical_review_flags=list(valuation.historical_review_flags),
                   historical_option_status=valuation.historical_option_status,
                   historical_fund_reference_status=valuation.historical_fund_reference_status,
                   candidate_review_status=valuation.candidate_status,
                   historical_reference_only=valuation.historical_reference_only,
                   historical_reference_double_counting=valuation.historical_reference_double_counting,
                   historical_reference_bands=list(valuation.historical_reference_bands))
        if valuation.candidate is not None:
            row["candidate"] = dict(zip(("L", "M", "H"), valuation.candidate), label=valuation.candidate_label)
        if valuation.option_band is not None:
            row["option"] = dict(zip(("L", "M", "H"), valuation.option_band))
            if valuation.option_label is not None:
                row["option"]["label"] = valuation.option_label
            if valuation.v_opt is not None:
                row["option"]["v_opt"] = valuation.v_opt
            if valuation.option_status is not None:
                row["option"]["review_status"] = valuation.option_status
        valuations[identity.ticker] = valuation
    axes = collect_axes(projections, archived=archived)
    for row, identity in parsed:
        if identity is not None:
            projection = projections[identity.ticker]
            row['axis_status'] = projection['status']
            row['axis_details'] = projection['axes']
            row['axes'] = [a['name'] for a in projection['axes'] if a['role'] == 'priced']
            row['ref_axes'] = [a['name'] for a in projection['axes'] if a['role'] == 'reference']
            row['consensus'] = projection.get('own_consensus')

    # Step 2: the one quote batch, read. A failed attempt keeps the last good batch, each row showing its own
    # fetch time; with no batch at all every band stays empty.
    snap = quotes.read(quote_pool(frameworks_dir))
    quote_error = None if snap.error is None else quote_failure(snap.error, snap.batch)
    if snap.batch is not None:
        us_close = us_regular_close(snap.batch.fetched_at)
        for row, identity in parsed:
            if identity is not None:
                _price_row(row, identity.ticker, valuations.get(identity.ticker), snap.batch, us_close)

    # Step 3: earnings dates from the calendar cache, for the tickers it covers (archived ones are not). A broken
    # framework has no header day to judge by, so it carries none.
    if earnings_dates is not None:
        updates = {row["ticker"]: date.fromisoformat(row["framework_updated"]) if row["framework_updated"] else None
                   for row, _ in parsed if row["name"] is not None}
        versions = {row["ticker"]: version for (row, _), version in snapshots if row["name"] is not None}
        found = earnings_dates(updates, versions)
        for row, _ in parsed:
            if row["name"] is not None:
                row.update(found.get(row["ticker"], {}))

    return {
        "zones": [{"id": z, "label": ZONE_CN[z]} for z in ZONE_ORDER],
        "lynch_classes": [{"id": k, "label": v} for k, v in LYNCH_CN.items()],
        "quote_error": quote_error,
        "axes": axes,
        "axis_documents": [path.stem for path in list_axis_paths(frameworks_dir / 'axes')],
        "tickers": [row for row, _ in parsed],
    }


def quote_pool(frameworks_dir: Path) -> list[str]:
    """Every framework, archived ones included (展示归档 shows their prices): the one symbol list quotes cover."""
    return [p.stem for p in list_framework_paths(frameworks_dir)]


def framework_updates(frameworks_dir: Path) -> dict[str, date | None]:
    """Header research dates do not depend on how valuation results are stored."""
    updates, _ = framework_snapshots(frameworks_dir)
    return updates


def framework_snapshots(frameworks_dir: Path) -> tuple[dict[str, date | None], dict[str, FileVersion | None]]:
    """Header dates and versions from the same reads, including cached parses."""
    snapshots = _framework_identities(list_framework_paths(frameworks_dir))
    return ({identity.ticker: identity.updated for identity, _ in snapshots if identity is not None},
            {identity.ticker: version for identity, version in snapshots if identity is not None})


def _stat_version(st) -> FileVersion:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def file_version(path: Path) -> FileVersion | None:
    try:
        st = path.lstat()
        return _stat_version(st) if stat.S_ISREG(st.st_mode) else None
    except OSError:
        return None


def _snapshot_parse(parse: Callable[[Path], Any]) -> Callable[[Path], tuple[Any, FileVersion | None]]:
    def read(path: Path) -> tuple[Any, FileVersion | None]:
        before = file_version(path)
        value = parse(path)
        after = file_version(path)
        return value, before if before is not None and before == after else None
    return read


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


def _ticker_row(path: Path) -> tuple[dict[str, Any], FrameworkIdentity | None]:
    row: dict[str, Any] = dict.fromkeys(ROW_KEYS)
    row["ticker"] = path.stem
    row["stale_after_earnings"] = False
    try:
        md = path.read_text(encoding="utf-8")
        identity = parse_framework_identity(md, path.stem)
    except Exception as e:  # noqa: BLE001 — one bad file must not blank the whole list
        log.warning("framework parse failed: %s: %s", path.name, e)
        row["error"] = str(e)
        return row, None
    row.update(
        name=identity.name,
        market=market_bucket(identity.ticker),
        lynch_primary=identity.lynch_primary,
        lynch_secondary=identity.lynch_secondary,
        axes=[],
        consensus=None,
        framework_updated=identity.updated.isoformat() if identity.updated else None,
    )
    return row, identity


def _framework_identity_or_none(path: Path) -> FrameworkIdentity | None:
    try:
        return parse_framework_identity(path.read_text(encoding="utf-8"), path.stem)
    except (OSError, UnicodeError, ValueError) as e:
        log.warning("framework identity failed: %s: %s", path.name, e)
        return None


# Kept across reads for every caller: a kept parse equals a fresh one.
_framework_rows = _ParsedFiles(_snapshot_parse(_ticker_row))
_framework_identities = _ParsedFiles(_snapshot_parse(_framework_identity_or_none))


def _price_row(row: dict[str, Any], ticker: str, meta: CatalogValuation | None,
               batch: QuoteBatch, us_close: datetime) -> None:
    """The row's band: live price, fetch time, and zones by the daily digest's own rules."""
    if ticker not in batch.symbols:  # a framework added since the last fetch
        row["quote_error"] = WAITING_QUOTE
        return
    q = batch.quotes.get(ticker)
    if not _usable(q):  # logged once when the batch was fetched
        row["quote_error"] = NO_QUOTE
        return
    price, session = _latest_price(q, us_close)
    try:
        comparable = meta is not None and not meta.historical_reference_only
        zone = zone_of(price, meta.L, meta.M, meta.H) if comparable else None
        option_zone = zone_of(price, *meta.option_band) if comparable and meta.option_band is not None else None
        candidate_zone = zone_of(price, *meta.candidate) if comparable and meta.candidate is not None else None
    except ValueError as e:  # a +V band out of order is not caught at parse time
        log.warning("zones failed: %s: %s", ticker, e)
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


def _traded_quote(symbol: str, q: Quote, now: float) -> Quote | None:
    """In the opening auction the last price is the auction's indicative price, not a trade: the last trade is
    the previous close, so the list and the anomaly checks see that; without one there is no price yet."""
    if not in_opening_auction(market_bucket(symbol), now):
        return q
    return None if q.prev_close is None else replace(q, last=q.prev_close)


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
