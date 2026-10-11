# App Anomaly Alerts + Single Quote Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One background-fetched quote copy that the home list and a new anomaly detector both read, plus an anomaly page behind the ⋯ menu.

**Architecture:** `QuoteCache` becomes the only `longbridge quote` caller, driven by a `QuotePoller` thread every 5 minutes (skipped while no pool market trades), by 刷新股价, and by the first read after start. Each new batch is handed to `Anomalies.observe`, which compares every price with its own base close against a per-ticker baseline (60-day stdev of daily returns, 60-day median volume) cached once per Beijing day. The once-per-day cache policy shared with the earnings calendar is extracted into `DailyTickerCache`. Events persist in `output/anomalies/events.json`; the web app lists the newest event per ticker.

**Tech Stack:** Python 3.12 + Starlette (app/server, pytest via `uv run`), stdlib unittest (eval/lib), React 19 + TypeScript + vitest (app/web), longbridge CLI 0.29.

**Spec:** `docs/superpowers/specs/2026-10-11-app-anomalies-design.md`

## Global Constraints

- A move counts when |price ÷ base − 1| ÷ sigma ≥ 4 (any session), or in the regular session ≥ 2 with today's volume ≥ 5 × median volume. Pre / post / overnight never use volume.
- sigma = population stdev of the last 60 daily close-to-close returns; volume baseline = median of the same 60 sessions; only closed bars (the bar dated the market's local today is dropped); fewer than 61 closed bars → no judgement.
- One event per (ticker, base close); the base close is the session's own `prev_close`, keyed as `repr(float)`.
- Judge only the price at fetch time; never between-poll highs / lows.
- Quote poll every 300 s, only when a pool market traded since the last batch (`market_hours.traded_between`) or the pool gained a ticker; 刷新股价 skips a fetch when the last attempt is younger than 60 s.
- A failed quote attempt keeps the last good batch and reports `取价失败：…（显示的是 HH:MM 的现价）`.
- Baseline and calendar: full fetch at most once per Beijing day, new tickers alone, 30 minutes back-off after a failure; fetched by the poller only, never on a request.
- An event first fired while its ticker is on the earnings list (judge(...) is not None) is filed under earnings forever: never on the anomaly page, shown on the earnings row.
- Anomaly page: newest non-earnings event per ticker, first fired within 7 days, newest first; events are dropped 7 days after their last hit.
- Researched = a `kind == "ticker"` chat on the ticker created at or after `first_at`; ignore is per event and undoable.
- Archived frameworks are fetched (the list shows them) but never judged.
- Home list re-requests `/api/tickers` every 5 minutes while on `#/`; `/api/anomalies` every 5 minutes on home and the anomaly page; one shared 5-minute constant.
- Times on the anomaly page are Beijing time.
- `output/anomalies/` is written only by the server, atomically.
- User-facing copy is Simplified Chinese; identifiers and comments English.
- Never write to `/tmp` (use `./_local/tmp/`); never stage `_local/`, `.mcp.json`, `config/mcporter.json`, `config/daily.toml`.

## Review Focus

1. A quote listener that raises (corrupt events file mid-write, bad baseline) must not break the quote fetch or `/api/tickers` — Task 3 test `test_listener_hears_each_new_batch_and_cannot_break_it`.
2. Server restart with an existing `events.json`: no repeated alerts, ignores kept — Task 4 test `test_events_and_ignores_survive_a_restart`.
3. A suspended ticker whose stale quote keeps matching stays one event and is not pruned while it still hits — Task 4 test `test_a_stale_quote_stays_one_event_and_is_kept_while_hit`.
4. Corrupt `events.json` or `baseline.json` starts empty instead of failing every request — Task 4 test `test_corrupt_files_start_empty`.
5. `GET /api/anomalies` never calls `longbridge kline` (baseline fetch is the poller's job) — Task 5 test `test_anomaly_list_never_fetches_bars`.

---

### Task 1: Quote volume, per-session base close, daily bars

**Files:**
- Modify: `eval/lib/price.py`
- Test: `eval/lib/test_price.py`

**Interfaces:**
- Produces: `Quote.volume: float | None` (last field, default None); `SessionQuote.prev_close: float | None` (last field, default None); `DailyBar(at: datetime, close: float, volume: float)`; `fetch_daily_bars(symbol: str, count: int, *, timeout_s: float = 60.0) -> list[DailyBar]` (oldest first; raises `RuntimeError` on CLI failure or a non-list payload).

- [ ] **Step 1: Write the failing tests**

Append to `eval/lib/test_price.py` (and extend `ROWS`):

```python
# in ROWS[0] (AAPL.US) add:  "volume": 41234567,
#   and give its blocks a base close: "pre_market": {..., "prev_close": "335.920"},
#   "post_market": {..., "prev_close": "341.070"}
# in ROWS[1] (700.HK) add:   "volume": 20422500,
```

```python
from eval.lib.price import DailyBar, SessionQuote, fetch_daily_bars, fetch_quotes


def run_cli(stdout: str, code: int = 0):
    done = subprocess.CompletedProcess([], code, stdout=stdout, stderr="boom" if code else "")
    return mock.patch("eval.lib.price.subprocess.run", return_value=done), \
        mock.patch("eval.lib.price.shutil.which", return_value="/bin/longbridge")


class QuoteFieldTests(unittest.TestCase):
    def test_volume_and_session_base_close(self) -> None:
        q = fetch(ROWS)
        self.assertEqual(q["AAPL.US"].volume, 41234567)
        self.assertEqual(q["700.HK"].volume, 20422500)
        self.assertEqual([x.prev_close for x in q["AAPL.US"].extended], [335.92, 341.07])


BARS = [
    {"time": "2026-10-08T04:00:00Z", "open": "1", "high": "1", "low": "1", "close": "31.370", "volume": "100"},
    {"time": "2026-10-09T04:00:00Z", "open": "1", "high": "1", "low": "1", "close": "31.020", "volume": "700"},
    {"time": "2026-10-10T04:00:00Z", "close": None, "volume": "1"},  # unparseable: dropped
]


class DailyBarTests(unittest.TestCase):
    def test_bars_oldest_first_with_their_own_time(self) -> None:
        run, which = run_cli(json.dumps(BARS))
        with run as r, which:
            bars = fetch_daily_bars("WOLF.US", 62)
        self.assertEqual(bars, [
            DailyBar(datetime(2026, 10, 8, 4, tzinfo=timezone.utc), 31.37, 100.0),
            DailyBar(datetime(2026, 10, 9, 4, tzinfo=timezone.utc), 31.02, 700.0),
        ])
        self.assertEqual(r.call_args.args[0], ["longbridge", "kline", "WOLF.US", "--period", "day", "--count", "62",
                                               "--adjust", "forward", "--format", "json"])

    def test_cli_failure_raises(self) -> None:
        run, which = run_cli("", code=1)
        with run, which, self.assertRaises(RuntimeError):
            fetch_daily_bars("WOLF.US", 62)

    def test_non_list_payload_raises(self) -> None:
        run, which = run_cli(json.dumps({"message": "token expired"}))
        with run, which, self.assertRaises(RuntimeError):
            fetch_daily_bars("WOLF.US", 62)
```

Keep the existing `test_extended_blocks_with_a_trade_time` assertion but give the expected `SessionQuote`s their base close: `SessionQuote("pre", 336.01, datetime(...), 335.92)` and `SessionQuote("post", 341.46, datetime(...), 341.07)`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest eval.lib.test_price -v`
Expected: FAIL / ImportError on `DailyBar`, `fetch_daily_bars`.

- [ ] **Step 3: Implement**

In `eval/lib/price.py`:

```python
@dataclass(frozen=True)
class SessionQuote:
    """A US extended-hours last price; `at` is its trade time (aware, UTC)."""

    session: str  # a key of EXTENDED_SESSIONS
    last: float
    at: datetime
    prev_close: float | None = None  # the regular close this session's moves are measured from


@dataclass(frozen=True)
class Quote:
    symbol: str
    last: float  # regular session only
    prev_close: float | None
    status: str | None
    extended: tuple[SessionQuote, ...] = ()
    volume: float | None = None  # today's regular-session volume, in the market's own unit (A shares: lots)


@dataclass(frozen=True)
class DailyBar:
    at: datetime  # the bar's own time (aware): local midnight for HK / A shares, 04:00 UTC for US
    close: float
    volume: float  # same unit as Quote.volume
```

Extract the CLI run that `fetch_quotes` does inline into one helper used by both fetchers:

```python
def _run(cmd: list[str], timeout_s: float) -> Any:
    if not shutil.which("longbridge"):
        raise RuntimeError("longbridge CLI not found on PATH")
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(
            f"longbridge {cmd[1]} failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    raw = proc.stdout.strip()
    if not raw:
        raise RuntimeError(f"longbridge {cmd[1]} returned empty stdout")
    return json.loads(raw)
```

`fetch_quotes` keeps its `if not symbols: return {}` guard, then `data = _run(["longbridge", "quote", *symbols, "--format", "json"], timeout_s)`; the rest is unchanged except the `Quote(...)` call gains `volume=_parse_float(row.get("volume"))`, and `_extended` passes `prev_close=_parse_float(block.get("prev_close"))` to `SessionQuote`.

```python
def fetch_daily_bars(symbol: str, count: int, *, timeout_s: float = 60.0) -> list[DailyBar]:
    """The latest `count` regular-session daily bars, forward-adjusted, oldest first; the last may still be open."""
    data = _run(["longbridge", "kline", symbol, "--period", "day", "--count", str(count),
                 "--adjust", "forward", "--format", "json"], timeout_s)
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected kline payload: {str(data)[:200]}")
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        close, volume, ts = _parse_float(row.get("close")), _parse_float(row.get("volume")), row.get("time")
        if close is None or volume is None or not ts:
            continue
        out.append(DailyBar(datetime.fromisoformat(str(ts).replace("Z", "+00:00")), close, volume))
    return out
```

Add `from typing import Any`. Update the module docstring to "Fetch quotes and daily bars via longbridge CLI."

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest discover -s eval/lib -t . -p 'test_*.py'`
Expected: OK (all eval tests, including the daily digest's).

- [ ] **Step 5: Commit**

```bash
git add eval/lib/price.py eval/lib/test_price.py
git commit -m "feat(eval): quotes carry volume and each session's base close; daily bar fetcher"
```

---

### Task 2: Extract the once-per-Beijing-day cache

**Files:**
- Create: `app/server/lynch_reader/daily_cache.py`
- Modify: `app/server/lynch_reader/earnings.py`
- Test: `app/server/tests/test_earnings.py`

**Interfaces:**
- Produces: `TickerFetch[T](data: dict[str, T], failed: list[str], error: str | None)`; `DailyTickerCache[T](path, wall)` with `get(pool, *, refresh=True) -> tuple[dict[str, T], str | None, str | None]` (data only for tickers it holds) and subclass hooks `_fetch(tickers, today) -> TickerFetch[T]`, `_encode(value) -> Any`, `_decode(raw) -> T`; module names `RETRY_S`, `BJT`, `write_json(path, data)`.
- `CalendarCache.get(pool, *, refresh=True)` keeps its old return shape (every pool ticker, `[]` when unknown). `refresh=False` never fetches.

- [ ] **Step 1: Write the failing test**

Append to `app/server/tests/test_earnings.py`:

```python
def test_read_only_get_never_fetches(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    assert cache.get(["TSM.US"], refresh=False) == ({"TSM.US": []}, None, None)
    assert cal.calls == []
    cache.get(["TSM.US"])
    clk.set(at("A", 10, 15, 8))  # a new Beijing day: a refreshing get would refetch
    events, day, _ = cache.get(["TSM.US"], refresh=False)
    assert len(cal.calls) == 1 and day == "2026-10-14" and events["TSM.US"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd app/server && uv run pytest -q tests/test_earnings.py::test_read_only_get_never_fetches`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'refresh'`.

- [ ] **Step 3: Implement**

Create `app/server/lynch_reader/daily_cache.py`:

```python
"""Per-ticker longbridge data fetched in full at most once per Beijing day, persisted across restarts.

The earnings calendar (earnings.py) and the anomaly baseline (anomalies.py) share this policy: between full
fetches, tickers never seen (a new framework) are fetched alone and tickers that left the pool are only filtered.
Any failure keeps the old data, holds the day open for a full refetch, and is not retried for RETRY_S.
"""
from __future__ import annotations

import json
import logging
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
BJT = MARKET_TZ["A"]  # the fetch day is a Beijing day: the user's day


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
    """See the module docstring. The lock makes concurrent callers wait for one fetch."""

    def __init__(self, path: Path, wall: Callable[[], float] = time.time) -> None:
        self._path, self._wall = path, wall
        self._lock = threading.Lock()
        self._day, self._data = self._load()
        self._failed_at: float | None = None
        self._error: str | None = None

    def get(self, pool: list[str], *, refresh: bool = True) -> tuple[dict[str, T], str | None, str | None]:
        """(data of the pool tickers it holds, last full fetch day, last error). `refresh=False` only reads."""
        with self._lock:
            if refresh:
                self._maybe_refresh(pool)
            return {t: self._data[t] for t in pool if t in self._data}, self._day, self._error

    def _maybe_refresh(self, pool: list[str]) -> None:
        now = self._wall()
        today = datetime.fromtimestamp(now, BJT).date()
        if self._failed_at is not None and now - self._failed_at < RETRY_S:
            return
        if self._day != today.isoformat():
            self._refresh(pool, today, now, full=True)
        elif new := [t for t in pool if t not in self._data]:
            self._refresh(new, today, now, full=False)

    def _refresh(self, tickers: list[str], today: date, now: float, *, full: bool) -> None:
        try:
            got = self._fetch(tickers, today)
        except Exception as e:  # noqa: BLE001 — the fetcher reports CLI failures itself; this is the backstop
            log.warning("%s fetch raised: %s", type(self).__name__, e)
            got = TickerFetch({}, list(tickers), str(e))
        self._data.update(got.data)
        if got.failed:
            self._failed_at = now
            self._error = f"{got.error}（{len(got.failed)} 只票未更新）"
        else:
            self._failed_at, self._error = None, None
            if full:
                self._day = today.isoformat()
        self._save()

    def _load(self) -> tuple[str | None, dict[str, T]]:
        if not self._path.exists():
            return None, {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw["day"], {t: self._decode(v) for t, v in raw["data"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("%s unreadable, starting empty: %s", self._path.name, e)  # refetched on the next refresh
            return None, {}

    def _save(self) -> None:
        write_json(self._path, {"day": self._day, "data": {t: self._encode(v) for t, v in self._data.items()}})

    def _fetch(self, tickers: list[str], today: date) -> TickerFetch[T]:
        raise NotImplementedError

    def _encode(self, value: T) -> Any:
        raise NotImplementedError

    def _decode(self, raw: Any) -> T:
        raise NotImplementedError
```

In `app/server/lynch_reader/earnings.py`: remove `RETRY_S`, `BJT`, `_write_json` and the whole old `CalendarCache` body; import them instead and subclass:

```python
from lynch_reader.daily_cache import BJT, RETRY_S, DailyTickerCache, TickerFetch, write_json  # noqa: F401 — RETRY_S re-exported for tests
```

```python
class CalendarCache(DailyTickerCache[list[CalendarEvent]]):
    """The pool's earnings calendar, refreshed by the policy in daily_cache.py."""

    def __init__(self, path: Path, fetch: Fetch = fetch_calendar, wall: Callable[[], float] = time.time) -> None:
        self._calendar = fetch
        super().__init__(path, wall)

    def get(self, pool: list[str], *, refresh: bool = True) -> tuple[dict[str, list[CalendarEvent]], str | None, str | None]:
        events, day, error = super().get(pool, refresh=refresh)
        return {t: events.get(t, []) for t in pool}, day, error

    def _fetch(self, tickers: list[str], today: date) -> TickerFetch[list[CalendarEvent]]:
        # One extra day back: a US market's local day trails Beijing's, so its 7th day is Beijing's 8th.
        start, end = today - timedelta(days=RELEASED_DAYS + 1), today + timedelta(days=LOOKAHEAD_DAYS)
        got = self._calendar(tickers, start, end)
        return TickerFetch(got.events, got.failed, got.error)

    def _encode(self, value: list[CalendarEvent]) -> Any:
        return [{"date": e.date, "slot": e.slot, "period": e.period} for e in value]

    def _decode(self, raw: Any) -> list[CalendarEvent]:
        return [CalendarEvent(**e) for e in raw]
```

`IgnoreStore._write` calls `write_json` instead of `_write_json`. Drop imports that became unused in `earnings.py` (`json` stays for `IgnoreStore.keys`; `os`, `threading`, `log` usage — remove only what the linter-free read shows unused). The calendar file's top-level key changes from `events` to `data`: an old file reads as unreadable and is refetched once (ledger this).

- [ ] **Step 4: Run the earnings tests**

Run: `cd app/server && uv run pytest -q tests/test_earnings.py tests/test_web.py`
Expected: all pass (the old calendar tests prove the policy moved intact).

- [ ] **Step 5: Commit**

```bash
git add app/server/lynch_reader/daily_cache.py app/server/lynch_reader/earnings.py app/server/tests/test_earnings.py
git commit -m "refactor(app): once-per-Beijing-day ticker cache shared by calendar and, next, anomaly baselines"
```

---

### Task 3: One quote copy, fetched in the background

**Files:**
- Modify: `app/server/lynch_reader/catalog.py`
- Test: `app/server/tests/test_catalog.py`, `app/server/tests/test_web.py`

**Interfaces:**
- Consumes: `traded_between`, `market_bucket`, `list_framework_paths` (existing).
- Produces:
  - `QuoteSnapshot(batch: QuoteBatch | None, error: Exception | None)`
  - `QuoteCache.read(symbols) -> QuoteSnapshot` (fetches only when nothing was ever attempted), `.poll(symbols) -> None`, `.refresh(symbols) -> QuoteSnapshot`, `.subscribe(listener: Callable[[QuoteBatch], None])`
  - `quote_pool(frameworks_dir) -> list[str]` (every framework file stem, archived included)
  - `quote_failure(e: Exception, batch: QuoteBatch | None) -> str`
  - `QUOTE_POLL_S = 300.0`; `QuotePoller(quotes, symbols: Callable[[], list[str]], after: Callable[[], None] = ..., interval_s=QUOTE_POLL_S)` with `run_once()`, `start()`, `stop()`
  - Removed: `QUOTE_MAX_AGE_S`, `QUOTE_BACKOFF_S`, `QuoteFailure`, `QuoteCache.get`.

- [ ] **Step 1: Rewrite the quote-cache tests (failing)**

In `tests/test_catalog.py` change the import to
`FRAMEWORKS_DIR, NO_QUOTE, QUOTE_REFRESH_MAX_AGE_S, QuotePoller, QuoteCache, axis_path_in, catalog, framework_path`.
Delete `test_quotes_are_reused_up_to_the_max_age`, `test_refresh_refetches_quotes_older_than_the_refresh_max_age`, `test_a_changed_pool_is_refetched`, `test_whole_call_failure_backs_off_then_retries`, `test_failure_after_a_good_batch_does_not_serve_the_old_batch`, `test_max_age_follows_the_monotonic_clock_not_the_wall_clock`, `test_quotes_are_reused_while_no_market_has_traded`. Replace `test_one_batched_fetch_for_parsed_rows` and `test_no_usable_quote_is_logged_once_per_batch`, and add:

```python
def test_one_batched_fetch_for_every_framework(repo, fetch, quotes):
    catalog(repo, quotes)
    catalog(repo, quotes)  # a plain load only reads
    assert fetch.calls == [["AAA.US", "BBB.US", "0001.HK"]]


def test_no_usable_quote_is_logged_once_per_batch(repo, fetch, clock, quotes, caplog):
    del fetch.prices["AAA.US"]
    with caplog.at_level(logging.WARNING, logger="lynch_reader"):
        catalog(repo, quotes)
        catalog(repo, quotes)  # same batch, no new warning
        assert sum("AAA.US" in r.getMessage() for r in caplog.records) == 1
        clock.now += QUOTE_REFRESH_MAX_AGE_S
        catalog(repo, quotes, refresh=True)  # a new batch warns again
        assert sum("AAA.US" in r.getMessage() for r in caplog.records) == 2


def test_refresh_skips_a_fetch_younger_than_a_minute(repo, fetch, clock, quotes):
    catalog(repo, quotes)
    clock.now += QUOTE_REFRESH_MAX_AGE_S - 1
    catalog(repo, quotes, refresh=True)
    assert len(fetch.calls) == 1
    clock.now += 1
    fetch.prices["AAA.US"] = 26.0
    assert rows_by_ticker(repo, quotes)["AAA.US"]["band"]["price"] == 25.0  # a plain load never fetches
    catalog(repo, quotes, refresh=True)
    assert len(fetch.calls) == 2
    assert rows_by_ticker(repo, quotes)["AAA.US"]["band"]["price"] == 26.0


def test_poll_fetches_only_after_a_market_traded(repo, fetch, clock, quotes):
    # US-only pool. Friday's post-market has settled; Monday's overnight opens Sunday 20:00.
    pool = ["AAA.US", "BBB.US"]
    clock.now = ny(25, 20, 30)
    quotes.poll(pool)
    clock.now = ny(26, 12)
    quotes.poll(pool)
    assert len(fetch.calls) == 1
    clock.now = ny(27, 20, 30)
    quotes.poll(pool)
    assert len(fetch.calls) == 2


def test_poll_fetches_when_the_pool_gains_a_ticker(repo, fetch, clock, quotes):
    clock.now = ny(26, 12)  # Saturday: no market trades
    quotes.poll(["AAA.US"])
    quotes.poll(["AAA.US", "BBB.US"])
    assert fetch.calls == [["AAA.US"], ["AAA.US", "BBB.US"]]


def test_a_new_framework_waits_for_the_next_fetch(repo, fetch, quotes):
    catalog(repo, quotes)
    (repo / "CCC.US.md").write_text(framework_md("CCC.US", "Gamma", "`stalwart`"), encoding="utf-8")
    row = rows_by_ticker(repo, quotes)["CCC.US"]
    assert row["band"] is None and row["quote_error"] == "等待下一次取价"
    assert len(fetch.calls) == 1


def test_failure_keeps_the_last_good_batch(repo, fetch, clock, quotes):
    catalog(repo, quotes)
    clock.now += QUOTE_REFRESH_MAX_AGE_S
    fetch.error = RuntimeError("boom")
    out = catalog(repo, quotes, refresh=True)
    shown = datetime.fromtimestamp(NOW, ZoneInfo("Asia/Shanghai")).strftime("%H:%M")  # the first batch's time
    assert out["quote_error"] == f"取价失败：boom（显示的是 {shown} 的现价）"
    assert {r["ticker"]: r for r in out["tickers"]}["AAA.US"]["band"]["price"] == 25.0
    fetch.error = None
    clock.now += QUOTE_REFRESH_MAX_AGE_S
    assert catalog(repo, quotes, refresh=True)["quote_error"] is None


def test_listener_hears_each_new_batch_and_cannot_break_it(repo, fetch, clock, quotes):
    heard = []
    quotes.subscribe(lambda b: heard.append(b.quotes["AAA.US"].last))
    quotes.subscribe(lambda b: 1 / 0)  # a broken listener is logged, not raised
    catalog(repo, quotes)
    catalog(repo, quotes)
    assert heard == [25.0]
    assert rows_by_ticker(repo, quotes)["AAA.US"]["band"]["price"] == 25.0


def test_poller_round_polls_then_runs_upkeep_and_survives_errors(fetch, clock, quotes):
    order = []
    quotes.subscribe(lambda b: order.append("batch"))
    poller = QuotePoller(quotes, lambda: ["AAA.US"], after=lambda: order.append("upkeep"))
    poller.run_once()
    assert order == ["batch", "upkeep"]
    broken = QuotePoller(quotes, lambda: 1 / 0, after=lambda: order.append("upkeep"))
    broken.run_once()  # a failing round is logged; upkeep still runs
    assert order[-1] == "upkeep"


def test_poller_thread_runs_a_round_at_start_and_stops(fetch, quotes):
    ran = threading.Event()
    poller = QuotePoller(quotes, lambda: ["AAA.US"], after=ran.set, interval_s=3600)
    poller.start()
    assert ran.wait(5)
    poller.stop()
    assert len(fetch.calls) == 1
```

Keep `test_whole_call_failure_nulls_every_band` (no batch yet: every band empty) and `test_requests_waiting_on_a_failing_call_reuse_its_failure` (the waiter reads after an attempt: no second call) unchanged; keep `test_us_row_takes_the_newest_price_since_the_regular_close` unchanged.

In `tests/test_web.py` replace `test_refresh_query_uses_the_refresh_max_age` with:

```python
def test_refresh_query_asks_for_a_fetch(repo, tmp_path, fetch, clock, quotes):
    c = client(repo, tmp_path, quotes)
    c.get("/api/tickers")
    clock.now += QUOTE_REFRESH_MAX_AGE_S
    c.get("/api/tickers")
    assert len(fetch.calls) == 1
    c.get("/api/tickers?refresh=1")
    assert len(fetch.calls) == 2
```

- [ ] **Step 2: Run to verify failures**

Run: `cd app/server && uv run pytest -q tests/test_catalog.py tests/test_web.py`
Expected: ImportError on `QuotePoller` (collection fails).

- [ ] **Step 3: Implement in `catalog.py`**

Replace the quote constants and `QuoteFailure` / `QuoteCache` with:

```python
# 刷新股价 fetches only when the last attempt is older than this; repeated taps add no CLI calls.
QUOTE_REFRESH_MAX_AGE_S = 60.0
# The poller's pace; the home list and the anomaly checks follow the same 5 minutes (spec).
QUOTE_POLL_S = 300.0
QUOTE_TIMEOUT_S = 15
NO_QUOTE = "无报价（停牌或代码变更）"
WAITING_QUOTE = "等待下一次取价"
```

```python
@dataclass(frozen=True)
class QuoteSnapshot:
    batch: QuoteBatch | None  # the latest good batch; None until one has been fetched
    error: Exception | None  # the latest attempt's failure; None once one succeeds


class QuoteCache:
    """The one copy of the pool's quotes (AGENTS.md「app 对话」). Three things fetch: the poller (`poll`), a
    刷新股价 tap (`refresh`), and the first read before any attempt (`read`); everything else only reads.

    A failed attempt keeps the last good batch and reports its error beside it. The lock makes concurrent
    callers wait for one CLI call. Listeners hear every new batch on the fetching thread, under the lock.
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
        self._error: Exception | None = None
        self._tried_mono: float | None = None
        self._listeners: list[Callable[[QuoteBatch], None]] = []

    def subscribe(self, listener: Callable[[QuoteBatch], None]) -> None:
        self._listeners.append(listener)

    def read(self, symbols: list[str]) -> QuoteSnapshot:
        with self._lock:
            if self._tried_mono is None:  # just started: the first reader waits for the first batch
                self._fetch_batch(symbols)
            return QuoteSnapshot(self._batch, self._error)

    def poll(self, symbols: list[str]) -> None:
        with self._lock:
            b = self._batch
            if b is None or set(symbols) - set(b.symbols) or self._traded_since(b, symbols):
                self._fetch_batch(symbols)

    def refresh(self, symbols: list[str]) -> QuoteSnapshot:
        with self._lock:
            if self._tried_mono is None or self._monotonic() - self._tried_mono >= QUOTE_REFRESH_MAX_AGE_S:
                self._fetch_batch(symbols)
            return QuoteSnapshot(self._batch, self._error)

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
            # Stamped after the call returns, so callers queued behind it see a fresh attempt.
            self._tried_mono = self._monotonic()
        self._error = None
        for s in key:
            if not _usable(quotes.get(s)):
                log.warning("no usable quote: %s: %s", s, quotes.get(s))
        self._batch = QuoteBatch(key, quotes, self._wall(), self._monotonic())
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
```

Add `quote_pool` and make `catalog` read:

```python
def quote_pool(frameworks_dir: Path) -> list[str]:
    """Every framework, archived ones included (展示归档 shows their prices): the one symbol list quotes cover."""
    return [p.stem for p in list_framework_paths(frameworks_dir)]
```

Replace Step 2 of `catalog`:

```python
    # Step 2: the one quote batch, read (on 刷新股价, refreshed). A failed attempt keeps the last good batch,
    # each row showing its own fetch time; with no batch at all every band stays empty.
    pool = quote_pool(frameworks_dir)
    snap = quotes.refresh(pool) if refresh else quotes.read(pool)
    quote_error = None if snap.error is None else quote_failure(snap.error, snap.batch)
    if snap.batch is not None:
        us_close = us_regular_close(snap.batch.fetched_at)
        for row, meta in priced:
            _price_row(row, meta, snap.batch, us_close)
```

In `_price_row`, before the `_usable` check:

```python
    if meta.ticker not in batch.symbols:  # a framework added since the last fetch
        row["quote_error"] = WAITING_QUOTE
        return
```

Rename `_quote_failure` to `quote_failure(e, batch)`:

```python
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
```

The timeout banner text changes from `取价超时（15 秒）` only by gaining the suffix when a batch exists; check `test_whole_call_failure_nulls_every_band` still matches. Update `__all__` (add `QuotePoller`, `QuoteSnapshot`, `quote_pool`, `quote_failure`) and the comment above `QuoteBatch`. `fetched_mono` in `QuoteBatch` is now unused: remove the field and its argument.

- [ ] **Step 4: Run tests**

Run: `cd app/server && uv run pytest -q tests/test_catalog.py tests/test_web.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/server/lynch_reader/catalog.py app/server/tests/test_catalog.py app/server/tests/test_web.py
git commit -m "feat(app): one quote copy, polled in the background; a failed fetch keeps the last prices"
```

---

### Task 4: Anomaly detection, baseline and event store

**Files:**
- Create: `app/server/lynch_reader/anomalies.py`
- Test: `app/server/tests/test_anomalies.py`

**Interfaces:**
- Consumes: `DailyTickerCache`, `TickerFetch`, `BJT`, `write_json` (Task 2); `QuoteBatch` (Task 3); `Quote`, `SessionQuote`, `DailyBar`, `fetch_daily_bars` (Task 1); `CalendarCache`, `judge` (earnings).
- Produces:
  - `Baseline(sigma: float, volume: float)`; `baseline_from_bars(ticker, bars, now: datetime) -> Baseline | None`
  - `BaselineCache(path, fetch=fetch_daily_bars, wall=time.time, sleep=time.sleep)` — a `DailyTickerCache[Baseline | None]`
  - `Hit(move, mult, session, at, reason, volume_mult)`; `hits(q: Quote, base: Baseline, fetched_at: float) -> list[tuple[str, Hit]]`
  - `Event(ticker, ref, first_at, first, peak, last_hit, earnings, ignored=False)`
  - `AnomalyStore(path, wall)` with `record(found: list[tuple[str, str, Hit]], in_earnings: Callable[[str], bool])`, `set_ignored(ticker, ref, ignored)` (KeyError when unknown), `events() -> list[Event]`
  - `Anomalies(store, baselines, calendar, wall)` with `observe(batch, pool)`, `upkeep(pool)`, `snapshot(pool, chat_starts, quote_error: str | None) -> dict`, `earnings_tag(ticker) -> dict | None`
  - `EVENTS_FILE`, `BASELINE_FILE` under `REPO_ROOT/output/anomalies/`

- [ ] **Step 1: Write the failing tests**

Create `app/server/tests/test_anomalies.py`:

```python
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from lynch_reader.anomalies import (
    Anomalies, AnomalyStore, Baseline, BaselineCache, Hit, baseline_from_bars, hits,
)
from lynch_reader.catalog import QuoteBatch
from lynch_reader.earnings import CalendarCache
from lynch_reader.market_hours import MARKET_TZ
from eval.lib.earnings_lb import CalendarEvent, CalendarFetch  # after lynch_reader: repo root on sys.path
from eval.lib.price import DailyBar, Quote, SessionQuote
from tests.conftest import NOW

BASE = Baseline(sigma=0.01, volume=1000.0)


def weekdays_until(last: date, n: int) -> list[date]:
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def us_bars(n: int = 62, last: date = date(2026, 9, 18), volume: float = 1000.0) -> list[DailyBar]:
    """Closes alternating ±1%: sigma 0.01. US bars sit at 04:00 UTC = local midnight."""
    out, close = [], 100.0
    for i, d in enumerate(weekdays_until(last, n)):
        if i:
            close *= 1.01 if i % 2 else 0.99
        out.append(DailyBar(datetime(d.year, d.month, d.day, 4, tzinfo=timezone.utc), close, volume))
    return out


NY_NOW = datetime.fromtimestamp(NOW, MARKET_TZ["US"])  # Mon 2026-09-21 10:13


# ---- baseline

def test_baseline_from_closed_bars():
    b = baseline_from_bars("AAA.US", us_bars(), NY_NOW)
    assert b.sigma == pytest.approx(0.01, rel=1e-6) and b.volume == 1000.0


def test_todays_open_bar_is_left_out():
    bars = us_bars(61) + [DailyBar(datetime(2026, 9, 21, 4, tzinfo=timezone.utc), 500.0, 9e9)]
    b = baseline_from_bars("AAA.US", bars, NY_NOW)
    assert b.sigma == pytest.approx(0.01, rel=1e-6) and b.volume == 1000.0


def test_fewer_than_61_closed_bars_is_no_baseline():
    assert baseline_from_bars("AAA.US", us_bars(60), NY_NOW) is None


def test_hk_bars_are_dated_in_hong_kong():
    # HK bars carry local midnight as 16:00 UTC the day before.
    days = weekdays_until(date(2026, 9, 18), 62)
    bars = [DailyBar(datetime(d.year, d.month, d.day, tzinfo=MARKET_TZ["HK"]).astimezone(timezone.utc),
                     100.0 * (1.01 if i % 2 else 1.0), 5.0) for i, d in enumerate(days)]
    assert baseline_from_bars("0700.HK", bars, NY_NOW) is not None


class FakeBars:
    def __init__(self, bars):
        self.bars, self.calls, self.fail = bars, [], set()

    def __call__(self, symbol, count):
        self.calls.append((symbol, count))
        if symbol in self.fail:
            raise RuntimeError("longbridge kline failed (exit 1): token expired")
        return self.bars


def test_baseline_cache_once_per_day_and_records_too_short(tmp_path):
    fake = FakeBars(us_bars())
    cache = BaselineCache(tmp_path / "baseline.json", fetch=fake, wall=lambda: NOW, sleep=lambda s: None)
    got, day, error = cache.get(["AAA.US", "BBB.US"])
    assert got["AAA.US"].sigma == pytest.approx(0.01, rel=1e-6) and error is None
    assert [c[1] for c in fake.calls] == [62, 62]
    cache.get(["AAA.US", "BBB.US"])
    assert len(fake.calls) == 2
    fake.bars = us_bars(10)
    cache2 = BaselineCache(tmp_path / "b2.json", fetch=fake, wall=lambda: NOW, sleep=lambda s: None)
    assert cache2.get(["CCC.US"])[0] == {"CCC.US": None}  # fetched, too short: never judged, not refetched today


def test_baseline_cache_failure_keeps_others(tmp_path):
    fake = FakeBars(us_bars())
    fake.fail = {"BBB.US"}
    got, _, error = BaselineCache(tmp_path / "baseline.json", fetch=fake, wall=lambda: NOW,
                                  sleep=lambda s: None).get(["AAA.US", "BBB.US"])
    assert "AAA.US" in got and "BBB.US" not in got and "token expired" in error


# ---- judging one quote

def q(last, prev=100.0, volume=None, extended=()):
    return Quote("AAA.US", last, prev, "Normal", extended, volume)


def test_four_times_the_usual_move_fires_and_3_9_does_not():
    [(ref, hit)] = hits(q(104.0), BASE, NOW)
    assert ref == "100.0" and (hit.reason, hit.session, round(hit.mult, 6)) == ("move", "regular", 4.0)
    assert hits(q(103.9), BASE, NOW) == []
    assert hits(q(96.0), BASE, NOW)[0][1].move == pytest.approx(-0.04)


def test_volume_rule_only_in_the_regular_session():
    [(_, hit)] = hits(q(102.5, volume=5000.0), BASE, NOW)
    assert (hit.reason, hit.volume_mult) == ("volume", 5.0)
    assert hits(q(102.5, volume=4999.0), BASE, NOW) == []
    assert hits(q(101.9, volume=9000.0), BASE, NOW) == []  # under 2x the usual move
    post = SessionQuote("post", 102.5, datetime(2026, 9, 21, 21, tzinfo=timezone.utc), 100.0)
    assert hits(q(100.0, volume=9000.0, extended=(post,)), BASE, NOW) == []


def test_extended_price_is_measured_from_its_own_base_close():
    # WOLF 10-07: closed 31.37, post-market printed 40.50 (+29%, sigma 7%).
    post = SessionQuote("post", 40.5, datetime(2026, 10, 7, 20, 5, tzinfo=timezone.utc), 31.37)
    [(ref, hit)] = hits(Quote("WOLF.US", 31.37, 31.0, "Normal", (post,), 1.0), Baseline(0.07, 1e6), NOW)
    assert ref == "31.37" and hit.session == "post" and hit.at == post.at.timestamp()
    assert hit.mult == pytest.approx(0.291 / 0.07, rel=1e-2)


def test_missing_base_or_bad_price_is_skipped():
    assert hits(q(104.0, prev=None), BASE, NOW) == []
    assert hits(q(float("nan")), BASE, NOW) == []


# ---- events

def hit(move, at=NOW, session="regular", reason="move", vm=None):
    return Hit(move, abs(move) / 0.01, session, at, reason, vm)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def test_one_event_per_base_close_with_the_peak_kept(tmp_path):
    clk = Clock()
    store = AnomalyStore(tmp_path / "events.json", wall=clk)
    store.record([("AAA.US", "100.0", hit(0.05))], lambda t: False)
    clk.now += 300
    store.record([("AAA.US", "100.0", hit(0.08)), ("AAA.US", "100.0", hit(0.06))], lambda t: True)
    [e] = store.events()
    assert e.first.move == 0.05 and e.peak.move == 0.08 and e.first_at == NOW and e.last_hit == NOW + 300
    assert e.earnings is False  # decided once, at the first trigger
    store.record([("AAA.US", "108.0", hit(-0.05))], lambda t: False)
    assert len(store.events()) == 2


def test_a_stale_quote_stays_one_event_and_is_kept_while_hit(tmp_path):
    clk = Clock()
    store = AnomalyStore(tmp_path / "events.json", wall=clk)
    for _ in range(10):  # a suspended ticker's last move, seen every day for ten days
        store.record([("AAA.US", "100.0", hit(0.2))], lambda t: False)
        clk.now += 86400
    assert len(store.events()) == 1
    clk.now += 8 * 86400  # no longer hit for over 7 days: dropped
    store.record([], lambda t: False)
    assert store.events() == []


def test_events_and_ignores_survive_a_restart(tmp_path):
    store = AnomalyStore(tmp_path / "events.json", wall=lambda: NOW)
    store.record([("AAA.US", "100.0", hit(0.05))], lambda t: False)
    store.set_ignored("AAA.US", "100.0", True)
    again = AnomalyStore(tmp_path / "events.json", wall=lambda: NOW)
    again.record([("AAA.US", "100.0", hit(0.05))], lambda t: False)
    [e] = again.events()
    assert e.ignored is True and e.first_at == NOW


def test_unknown_event_cannot_be_ignored(tmp_path):
    with pytest.raises(KeyError):
        AnomalyStore(tmp_path / "events.json", wall=lambda: NOW).set_ignored("AAA.US", "1.0", True)


def test_corrupt_files_start_empty(tmp_path):
    (tmp_path / "events.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "baseline.json").write_text("[1, 2", encoding="utf-8")
    assert AnomalyStore(tmp_path / "events.json", wall=lambda: NOW).events() == []
    cache = BaselineCache(tmp_path / "baseline.json", fetch=FakeBars(us_bars()), wall=lambda: NOW, sleep=lambda s: None)
    assert cache.get(["AAA.US"], refresh=False) == ({}, None, None)


# ---- the facade

def calendar(tmp_path, events=None):
    def fetch(tickers, start, end):
        return CalendarFetch({t: (events or {}).get(t, []) for t in tickers}, [], None)
    return CalendarCache(tmp_path / "calendar.json", fetch=fetch, wall=lambda: NOW)


def facade(tmp_path, clk, cal=None):
    baselines = BaselineCache(tmp_path / "baseline.json", fetch=FakeBars(us_bars()), wall=clk, sleep=lambda s: None)
    a = Anomalies(AnomalyStore(tmp_path / "events.json", wall=clk), baselines, cal or calendar(tmp_path), wall=clk)
    a.upkeep(["AAA.US", "BBB.US"])
    return a


def batch(prices: dict[str, float], at=NOW):
    return QuoteBatch(tuple(prices), {t: Quote(t, p, 100.0, "Normal", (), None) for t, p in prices.items()}, at)


def test_observe_judges_only_the_given_pool(tmp_path):
    clk = Clock()
    a = facade(tmp_path, clk)
    a.observe(batch({"AAA.US": 110.0, "BBB.US": 120.0}), ["AAA.US"])  # BBB archived: left out of the pool
    items = a.snapshot(["AAA.US", "BBB.US"], [], None)["items"]
    assert [(i["ticker"], i["session"], i["reason"]) for i in items] == [("AAA.US", "regular", "move")]
    assert items[0]["move"] == pytest.approx(0.1) and items[0]["mult"] == pytest.approx(10.0, rel=1e-3)


def test_newest_event_per_ticker_and_pending_flags(tmp_path):
    clk = Clock()
    a = facade(tmp_path, clk)
    a.observe(batch({"AAA.US": 110.0}), ["AAA.US"])
    a.store.set_ignored("AAA.US", "100.0", True)
    clk.now += 86400
    # Next day: a new base close, a new event; the ignored one is no longer shown.
    a.observe(QuoteBatch(("AAA.US",), {"AAA.US": Quote("AAA.US", 99.0, 110.0, "Normal", (), None)}, clk.now),
              ["AAA.US"])
    [item] = a.snapshot(["AAA.US"], [], None)["items"]
    assert (item["ref"], item["ignored"], item["researched"]) == ("110.0", False, False)
    [item] = a.snapshot(["AAA.US"], [("AAA.US", clk.now + 1)], None)["items"]
    assert item["researched"] is True
    [item] = a.snapshot(["AAA.US"], [("AAA.US", clk.now - 1)], None)["items"]  # a chat from before it fired
    assert item["researched"] is False


def test_listed_for_seven_days_after_it_first_fired(tmp_path):
    clk = Clock()
    a = facade(tmp_path, clk)
    a.observe(batch({"AAA.US": 110.0}), ["AAA.US"])
    clk.now += 7 * 86400
    assert len(a.snapshot(["AAA.US"], [], None)["items"]) == 1
    clk.now += 1
    assert a.snapshot(["AAA.US"], [], None)["items"] == []


def test_ticker_on_the_earnings_list_files_its_anomaly_there(tmp_path):
    clk = Clock()
    # AAA released before Monday's open (NOW is Mon 10:13 New York): on the earnings list.
    cal = calendar(tmp_path, {"AAA.US": [CalendarEvent("2026-09-21", "before", "2026 财年第 3 季度")]})
    a = facade(tmp_path, clk, cal)
    a.observe(batch({"AAA.US": 110.0, "BBB.US": 110.0}), ["AAA.US", "BBB.US"])
    assert [i["ticker"] for i in a.snapshot(["AAA.US", "BBB.US"], [], None)["items"]] == ["BBB.US"]
    tag = a.earnings_tag("AAA.US")
    assert tag["move"] == pytest.approx(0.1) and "researched" not in tag
    assert a.earnings_tag("BBB.US") is None


def test_snapshot_reports_quote_or_baseline_errors(tmp_path):
    clk = Clock()
    a = facade(tmp_path, clk)
    assert a.snapshot(["AAA.US"], [], "取价失败：boom")["error"] == "取价失败：boom"


def test_item_shape(tmp_path):
    clk = Clock()
    a = facade(tmp_path, clk)
    a.observe(batch({"AAA.US": 110.0}), ["AAA.US"])
    [item] = a.snapshot(["AAA.US"], [], None)["items"]
    assert set(item) == {"ticker", "ref", "first_at", "move", "mult", "session", "at", "reason", "volume_mult",
                         "researched", "ignored"}
    assert item["first_at"] == datetime.fromtimestamp(NOW, MARKET_TZ["A"]).isoformat(timespec="seconds")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd app/server && uv run pytest -q tests/test_anomalies.py`
Expected: collection error, `ModuleNotFoundError: lynch_reader.anomalies`.

- [ ] **Step 3: Implement `app/server/lynch_reader/anomalies.py`**

```python
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

    def snapshot(self, pool: list[str], chat_starts: list[tuple[str, float]], quote_error: str | None) -> dict[str, Any]:
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
```

Note `Event` is frozen, so `record` and `set_ignored` replace entries.

- [ ] **Step 4: Run tests**

Run: `cd app/server && uv run pytest -q tests/test_anomalies.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/server/lynch_reader/anomalies.py app/server/tests/test_anomalies.py
git commit -m "feat(app): anomaly baselines once a day, judging every quote batch, events per base close"
```

---

### Task 5: Routes, wiring, lifespan, docs

**Files:**
- Modify: `app/server/lynch_reader/web.py`, `app/server/lynch_reader/earnings.py` (`Earnings.snapshot` gains `tag`), `AGENTS.md`
- Test: `app/server/tests/test_web.py`, `app/server/tests/test_earnings.py`, `app/server/tests/conftest.py`

**Interfaces:**
- Consumes: `QuoteCache.subscribe/read`, `QuotePoller`, `quote_pool`, `quote_failure` (Task 3); `Anomalies`, `AnomalyStore`, `BaselineCache`, `EVENTS_FILE`, `BASELINE_FILE` (Task 4).
- Produces: `active_pool(frameworks_dir, archive) -> list[str]`; `create_app(..., earnings=None, anomalies=None, poller=None)`; routes `GET /api/anomalies`, `POST /api/anomalies/ignore`, `POST /api/anomalies/unignore` (body `{ticker, ref}`); `GET /api/earnings` items gain `anomaly`; `Earnings.snapshot(pool, chat_starts, tag=lambda t: None)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_earnings.py` append:

```python
def test_snapshot_carries_each_rows_anomaly_tag(tmp_path, cal):
    clk = Clock(at("US", 10, 15, 10))
    store = IgnoreStore(tmp_path / "ignored.json", wall=clk)
    e = Earnings(CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk), store, wall=clk)
    [item] = e.snapshot(["TSM.US"], [], tag=lambda t: {"move": 0.1} if t == "TSM.US" else None)["items"]
    assert item["anomaly"] == {"move": 0.1}
    assert e.snapshot(["TSM.US"], [])["items"][0]["anomaly"] is None
```

In `tests/test_web.py`: extend imports and `client`:

```python
from lynch_reader.anomalies import Anomalies, AnomalyStore, BaselineCache, Hit
from tests.test_anomalies import FakeBars, us_bars
```

```python
def client(repo, tmp_path, quotes, with_dist=True, calendar=fake_calendar, bars=None, poller=None):
    ...  # existing body up to `earnings = ...`
    cal = CalendarCache(tmp_path / "earnings" / "calendar.json", fetch=calendar, wall=wall)
    earnings = Earnings(cal, IgnoreStore(tmp_path / "earnings" / "ignored.json", wall=wall), wall=wall)
    baselines = BaselineCache(tmp_path / "anomalies" / "baseline.json", fetch=bars or FakeBars(us_bars()),
                              wall=wall, sleep=lambda s: None)
    anomalies = Anomalies(AnomalyStore(tmp_path / "anomalies" / "events.json", wall=wall), baselines, cal, wall=wall)
    app = create_app(repo, dist, quotes, devices, archive, earnings=earnings, anomalies=anomalies, poller=poller)
    c = TestClient(app, cookies=cookies)
    c.anomalies = anomalies  # tests reach the store and the upkeep through the client
    return c
```

```python
def test_a_new_batch_is_judged_for_the_active_pool(repo, tmp_path, fetch, quotes):
    c = client(repo, tmp_path, quotes)
    c.anomalies.upkeep(["AAA.US", "BBB.US"])  # the poller's daily baseline fetch
    # BBB at 5.0 on a 4.0 base close is +25%; AAA (on the earnings list in fake_calendar) has no base close.
    fetch.prev["BBB.US"] = 4.0
    c.get("/api/tickers")  # the first read fetches; the listener judges the batch
    items = c.get("/api/anomalies").json()["items"]
    assert [(i["ticker"], i["ref"], i["reason"]) for i in items] == [("BBB.US", "4.0", "move")]
    c.post("/api/frameworks/BBB.US/archive", json={})
    assert c.get("/api/anomalies").json()["items"] == []  # archived: not listed


def test_anomaly_list_never_fetches_bars(repo, tmp_path, quotes):
    bars = FakeBars(us_bars())
    c = client(repo, tmp_path, quotes, bars=bars)
    body = c.get("/api/anomalies").json()
    assert body == {"error": None, "items": []} and bars.calls == []


def test_anomaly_ignore_and_unignore(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    c.anomalies.store.record([("AAA.US", "20.0", Hit(0.25, 25.0, "regular", NOW, "move", None))], lambda t: False)
    assert c.post("/api/anomalies/ignore", json={"ticker": "AAA.US", "ref": "20.0"}).status_code == 204
    assert c.get("/api/anomalies").json()["items"][0]["ignored"] is True
    assert c.post("/api/anomalies/unignore", json={"ticker": "AAA.US", "ref": "20.0"}).status_code == 204
    assert c.get("/api/anomalies").json()["items"][0]["ignored"] is False


def test_anomaly_ignore_refuses_bad_input(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    for body in ({"ticker": "NOPE.US", "ref": "20.0"}, {"ticker": "AAA.US", "ref": "1.0"},
                 {"ticker": "AAA.US"}, {"ticker": 1, "ref": "20.0"}):
        r = c.post("/api/anomalies/ignore", json=body)
        assert r.status_code == 400 and r.json()["error"], body
    r = c.post("/api/anomalies/ignore", content="x", headers={"content-type": "text/plain"})
    assert r.status_code == 415


def test_earnings_items_carry_their_anomaly(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    c.anomalies.store.record([("AAA.US", "20.0", Hit(0.25, 25.0, "post", NOW, "move", None))], lambda t: True)
    [item] = c.get("/api/earnings").json()["items"]
    assert item["anomaly"]["session"] == "post"
    assert c.get("/api/anomalies").json()["items"] == []


def test_lifespan_starts_and_stops_the_poller(repo, tmp_path, quotes):
    class FakePoller:
        started = stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True
    p = FakePoller()
    with client(repo, tmp_path, quotes, poller=p) as c:
        assert p.started and not p.stopped
        c.get("/api/tickers")
    assert p.stopped
```

In `tests/conftest.py`, give `FakeFetch` base closes: `self.prev: dict[str, float] = {}` in `__init__`, and `prev_close=self.prev.get(s)` in the `Quote(...)` it builds (other tests leave it empty, so their quotes keep `prev_close=None`).

- [ ] **Step 2: Run them to verify they fail**

Run: `cd app/server && uv run pytest -q tests/test_web.py tests/test_earnings.py`
Expected: failures — `create_app() got an unexpected keyword argument 'anomalies'`, `snapshot() got an unexpected keyword argument 'tag'`.

- [ ] **Step 3: Implement**

`earnings.py` — `Earnings.snapshot` and `_item`:

```python
    def snapshot(self, pool: list[str], chat_starts: list[tuple[str, float]],
                 tag: Callable[[str], dict[str, Any] | None] = lambda t: None) -> dict[str, Any]:
        ...
                "items": [_item(r, ignored, chat_starts, tag(r.ticker)) for r in released + upcoming]}


def _item(r: Row, ignored, chat_starts, anomaly: dict[str, Any] | None) -> dict[str, Any]:
    ...  # existing keys, plus:
        "anomaly": anomaly,  # the ticker's anomaly filed under earnings (anomalies.py), if any
```

`web.py`:

```python
from contextlib import asynccontextmanager
import asyncio

from lynch_reader.anomalies import BASELINE_FILE, EVENTS_FILE, Anomalies, AnomalyStore, BaselineCache
from lynch_reader.catalog import (FRAMEWORKS_DIR, REPO_ROOT, QuoteCache, QuotePoller, axis_path_in, catalog,
                                  framework_path, quote_failure, quote_pool)


def active_pool(frameworks_dir: Path, archive: FrameworkArchive) -> list[str]:
    """Frameworks the alerts cover: archived ones are left out."""
    hidden = archive.tickers()
    return [p.stem for p in list_framework_paths(frameworks_dir) if p.stem not in hidden]


def app_lifespan(chat: ChatService | None, poller: QuotePoller | None):
    @asynccontextmanager
    async def lifespan(app):
        if poller is not None:
            poller.start()
        try:
            if chat is not None:
                async with chat_lifespan(chat)(app):
                    yield
            else:
                yield
        finally:
            if poller is not None:
                await asyncio.to_thread(poller.stop)  # joins the thread: keep the event loop free meanwhile
    return lifespan
```

In `create_app` (new params `anomalies: Anomalies | None = None, poller: QuotePoller | None = None`):

```python
    pool = lambda: active_pool(frameworks_dir, archive)  # noqa: E731
    if anomalies is not None:
        quotes.subscribe(lambda batch: anomalies.observe(batch, pool()))
```

`earnings_list` uses `pool()` instead of its inline list and passes `tag=anomalies.earnings_tag if anomalies is not None else (lambda t: None)`.

```python
    def anomaly_list(request: Request) -> Response:
        # Sync: right after start the first read waits on the first quote fetch, in the thread pool.
        snap = quotes.read(quote_pool(frameworks_dir))
        error = None if snap.error is None else quote_failure(snap.error, snap.batch)
        starts = chat.ticker_chat_starts() if chat is not None else []
        return JSONResponse(anomalies.snapshot(pool(), starts, error))

    def set_anomaly_ignored(ignored: bool):
        async def run(request: Request) -> Response:
            body = await _body(request)
            ticker, ref = body.get("ticker"), body.get("ref")
            if not isinstance(ticker, str) or not isinstance(ref, str):
                raise Invalid("需要 ticker 和 ref")
            if framework_path(ticker, frameworks_dir) is None:
                raise Invalid("没有这只票的框架")
            try:
                anomalies.store.set_ignored(ticker, ref, ignored)
            except KeyError:
                raise Invalid("没有这条异动") from None
            return Response(status_code=204)
        return _guarded(run)  # its CSRF guard: a cross-site form must not dismiss anything
```

Routes:

```python
    if anomalies is not None:
        routes += [
            Route("/api/anomalies", anomaly_list),
            Route("/api/anomalies/ignore", set_anomaly_ignored(True), methods=["POST"]),
            Route("/api/anomalies/unignore", set_anomaly_ignored(False), methods=["POST"]),
        ]
```

`Starlette(..., lifespan=app_lifespan(chat, poller))`.

`main`:

```python
    quotes = QuoteCache()
    archive = FrameworkArchive(ARCHIVE_FILE)
    calendar = CalendarCache(CALENDAR_FILE)
    anomalies = Anomalies(AnomalyStore(EVENTS_FILE), BaselineCache(BASELINE_FILE), calendar)
    poller = QuotePoller(quotes, lambda: quote_pool(FRAMEWORKS_DIR),
                         after=lambda: anomalies.upkeep(active_pool(FRAMEWORKS_DIR, archive)))
    uvicorn.run(create_app(FRAMEWORKS_DIR, WEB_DIST, quotes, DeviceStore(DEVICES_FILE), archive, chat=chat,
                           earnings=Earnings(calendar, IgnoreStore(IGNORED_FILE)), anomalies=anomalies, poller=poller),
                host=args.host, port=args.port, timeout_graceful_shutdown=5)
```

Update the `web.py` module docstring's list of what the server writes to include `output/anomalies/`.

`AGENTS.md`, 「app 对话」 section — add after the 财报提示 bullet:

```markdown
- 现价与异动提示：现价只有一份——服务端后台每 5 分钟取一次全池现价（池里没有市场在交易、也没有新框架时不取；「刷新股价」让它立刻取一次，1 分钟内取过则不取），首页列表每 5 分钟读一次、异动判定在每批现价到达时做，都不自己取价。⋯ 菜单「异动」页每只票列出最新一条：现价（含盘前、盘后、夜盘）相对它所在时段对应的正式收盘，超过平时波动 4 倍，或正式时段超过 2 倍且当天成交量超过平时 5 倍（平时 = 近 60 个交易日的日涨跌幅标准差 / 成交量中位数，日线每个北京日由后台查一次）。首次触发时已在财报页上的票归财报，不进异动。判定与存储的唯一真源是 `app/server/lynch_reader/anomalies.py`，记录在 `output/anomalies/`（服务端唯一写者）；现价的唯一来源是 `catalog.py` 的 `QuoteCache` / `QuotePoller`。
```

- [ ] **Step 4: Run the whole server suite**

Run: `cd app/server && uv run pytest -q`
Expected: all pass (10 skips as before).

- [ ] **Step 5: Commit**

```bash
git add app/server/lynch_reader/web.py app/server/lynch_reader/earnings.py app/server/tests/test_web.py \
        app/server/tests/test_earnings.py app/server/tests/conftest.py AGENTS.md
git commit -m "feat(app): /api/anomalies, the background quote poller in the app lifespan, anomaly tags on earnings"
```

---

### Task 6: Web — anomaly page, menu row, prompts, 5-minute polling

**Files:**
- Create: `app/web/src/anomalies/types.ts`, `app/web/src/anomalies/api.ts`, `app/web/src/anomalies/model.ts`, `app/web/src/anomalies/model.test.ts`, `app/web/src/anomalies/AnomaliesPage.tsx`, `app/web/src/alerts/AlertRow.tsx`, `app/web/src/alerts/model.ts`
- Modify: `app/web/src/earnings/types.ts`, `app/web/src/earnings/model.ts`, `app/web/src/earnings/prompts.ts`, `app/web/src/earnings/prompts.test.ts`, `app/web/src/earnings/model.test.ts`, `app/web/src/earnings/EarningsPage.tsx`, `app/web/src/route.ts`, `app/web/src/route.test.ts`, `app/web/src/home/HomePage.tsx`, `app/web/src/App.tsx`

**Interfaces:**
- Consumes: `GET /api/anomalies` `{error, items: AnomalyItem[]}`; `POST /api/anomalies/(un)ignore` `{ticker, ref}`; `EarningsItem.anomaly: AnomalyHit | null`.
- Produces: route `{page: 'anomalies'}`, `anomaliesHash()`; `anomalyPrompt(it)`; `HomePage` props `anomaliesPending`, `onOpenAnomalies`.

- [ ] **Step 1: Write the failing tests**

`app/web/src/anomalies/model.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import { hitText, isPending, menuLabel, pendingCount } from './model';
import type { AnomalyItem } from './types';

const anomaly = (o: Partial<AnomalyItem> = {}): AnomalyItem => ({
  ticker: 'WOLF.US', ref: '31.37', first_at: '2026-10-08T04:05:00+08:00',
  move: 0.291, mult: 4.13, session: 'post', at: '2026-10-08T04:20:00+08:00',
  reason: 'move', volume_mult: null, researched: false, ignored: false, ...o,
});

describe('anomaly model', () => {
  it('pending means neither researched nor ignored', () => {
    expect(isPending(anomaly())).toBe(true);
    expect(isPending(anomaly({ researched: true }))).toBe(false);
    expect(isPending(anomaly({ ignored: true }))).toBe(false);
    expect(pendingCount({ error: null, items: [anomaly(), anomaly({ ignored: true })] })).toBe(1);
    expect(pendingCount(null)).toBe(0);
  });
  it('menu label counts what waits', () => {
    expect(menuLabel(2)).toBe('异动 · 2 待看');
    expect(menuLabel(0)).toBe('异动');
  });
  it('hit text: move, session, Beijing time, multiple, volume', () => {
    expect(hitText(anomaly())).toBe('+29.1% 盘后 · 10-08 04:20 · 平时波动 4.1 倍');
    expect(hitText(anomaly({ move: -0.063, session: 'regular', reason: 'volume', mult: 2.2, volume_mult: 5.34,
      at: '2026-10-08T23:10:00+08:00' }))).toBe('-6.3% · 10-08 23:10 · 平时波动 2.2 倍 · 放量 5.3 倍');
  });
});
```

Add to `app/web/src/earnings/prompts.test.ts` (and give its `item()` factory `anomaly: null`):

```ts
import { anomalyPrompt } from './prompts';
import type { AnomalyItem } from '../anomalies/types';

// A local factory: importing another test file would re-run its tests here.
const anomaly = (o: Partial<AnomalyItem> = {}): AnomalyItem => ({
  ticker: 'WOLF.US', ref: '31.37', first_at: '2026-10-08T04:05:00+08:00',
  move: 0.291, mult: 4.13, session: 'post', at: '2026-10-08T04:20:00+08:00',
  reason: 'move', volume_mult: null, researched: false, ignored: false, ...o,
});

  it('anomaly prompt names the move, its base and the no-write rule', () => {
    const p = anomalyPrompt(anomaly());
    expect(p).toContain('WOLF.US 在 10-08 04:20 盘后 出现异动：相对基准收盘 31.37 +29.1%（约平时波动的 4.1 倍）');
    expect(anomalyPrompt(anomaly({ session: 'regular' }))).toContain('WOLF.US 在 10-08 04:20 出现异动');
    expect(p).toContain('frameworks/WOLF.US.md');
    expect(p).toContain('落盘等我说「可以落盘」');
  });
  it('a volume anomaly states the volume multiple', () => {
    expect(anomalyPrompt(anomaly({ reason: 'volume', volume_mult: 5.34, session: 'regular' })))
      .toContain('；当天成交量是平时的 5.3 倍');
  });
  it('post-earnings prompt carries a filed anomaly', () => {
    const p = earningsPrompt(item({ anomaly: anomaly() }));
    expect(p.split('\n')[1]).toBe('财报前后出现异动：10-08 04:20 盘后 相对基准收盘 31.37 +29.1%（约平时波动的 4.1 倍），请一并解释。');
  });
```

Give `app/web/src/earnings/model.test.ts`'s item factory `anomaly: null` too. In `app/web/src/route.test.ts` add:

```ts
  it('parses the anomaly page', () => {
    expect(parseRoute('#/anomalies')).toEqual({ page: 'anomalies' });
    expect(anomaliesHash()).toBe('#/anomalies');
  });
```

(import `anomaliesHash` beside the existing imports.)

- [ ] **Step 2: Run them to verify they fail**

Run: `npm --prefix app/web test -- --run`
Expected: failures resolving `./model` in `anomalies/`, `anomalyPrompt`, `anomaliesHash`.

- [ ] **Step 3: Implement**

`app/web/src/anomalies/types.ts`:

```ts
export type AnomalySession = 'regular' | 'pre' | 'post' | 'overnight';

/** The largest move seen against one base close (server: anomalies.py). Times are Beijing ISO strings. */
export interface AnomalyHit {
  ticker: string;
  ref: string; // the base close, the event's key with the ticker
  first_at: string;
  move: number; // price / base close - 1
  mult: number; // |move| / the ticker's usual daily move
  session: AnomalySession;
  at: string;
  reason: 'move' | 'volume';
  volume_mult: number | null;
}

export interface AnomalyItem extends AnomalyHit {
  researched: boolean; // a 讨论 chat on the ticker was started after it first fired
  ignored: boolean;
}

export interface AnomalyList {
  error: string | null; // the latest quote fetch or baseline refresh failure
  items: AnomalyItem[]; // newest event per ticker, newest first
}
```

`app/web/src/anomalies/api.ts`:

```ts
import { get, post } from '../api';
import type { AnomalyList } from './types';

export async function fetchAnomalies(): Promise<AnomalyList> {
  return (await get('/api/anomalies')).json();
}

export async function ignoreAnomaly(ticker: string, ref: string): Promise<void> {
  await post('/api/anomalies/ignore', { ticker, ref });
}

export async function unignoreAnomaly(ticker: string, ref: string): Promise<void> {
  await post('/api/anomalies/unignore', { ticker, ref });
}
```

`app/web/src/alerts/model.ts`:

```ts
/** The grey tag on a done alert row, earnings or anomaly alike. */
export function doneTag(it: { researched: boolean; ignored: boolean }): string | null {
  if (it.researched) return '已调研';
  if (it.ignored) return '已忽略';
  return null;
}
```

In `app/web/src/earnings/model.ts` delete its `doneTag` and add `export { doneTag } from '../alerts/model';`.

`app/web/src/anomalies/model.ts`:

```ts
import type { AnomalyHit, AnomalyItem, AnomalyList, AnomalySession } from './types';

export const SESSION_CN: Record<AnomalySession, string> = { regular: '', pre: '盘前', post: '盘后', overnight: '夜盘' };

export function isPending(it: AnomalyItem): boolean {
  return !it.researched && !it.ignored;
}

export function pendingCount(list: AnomalyList | null): number {
  return list ? list.items.filter(isPending).length : 0;
}

export function menuLabel(n: number): string {
  return n > 0 ? `异动 · ${n} 待看` : '异动';
}

export function moveText(h: AnomalyHit): string {
  return `${h.move >= 0 ? '+' : ''}${(h.move * 100).toFixed(1)}%`;
}

/** "10-08 04:20": the server sends Beijing ISO times. */
export function atText(h: AnomalyHit): string {
  return `${h.at.slice(5, 10)} ${h.at.slice(11, 16)}`;
}

export function hitText(h: AnomalyHit): string {
  const session = SESSION_CN[h.session] ? ` ${SESSION_CN[h.session]}` : '';
  const volume = h.reason === 'volume' && h.volume_mult !== null ? ` · 放量 ${h.volume_mult.toFixed(1)} 倍` : '';
  return `${moveText(h)}${session} · ${atText(h)} · 平时波动 ${h.mult.toFixed(1)} 倍${volume}`;
}
```

`app/web/src/earnings/types.ts`: add `import type { AnomalyHit } from '../anomalies/types';` and `anomaly: AnomalyHit | null; // the ticker's anomaly filed under this row` to `EarningsItem`.

`app/web/src/earnings/prompts.ts` — add, and use in `earningsPrompt`:

```ts
import { atText, moveText, SESSION_CN } from '../anomalies/model';
import type { AnomalyHit, AnomalyItem } from '../anomalies/types';

/** "10-08 04:20 盘后" (Beijing time; the regular session adds nothing). */
function hitWhen(h: AnomalyHit): string {
  return SESSION_CN[h.session] ? `${atText(h)} ${SESSION_CN[h.session]}` : atText(h);
}

export function anomalyPrompt(it: AnomalyItem): string {
  const volume = it.reason === 'volume' && it.volume_mult !== null ? `；当天成交量是平时的 ${it.volume_mult.toFixed(1)} 倍` : '';
  return [
    `${it.ticker} 在 ${hitWhen(it)} 出现异动：相对基准收盘 ${it.ref} ${moveText(it)}（约平时波动的 ${it.mult.toFixed(1)} 倍${volume}）。请做异动核查：`,
    '1. 查清原因：新闻、公告、评级调整、行业或宏观事件（注明来源和时间）。',
    `2. 读 frameworks/${it.ticker}.md 的现行结论和 Assumptions，判断这次异动是否触及某条假设：确认 / 削弱 / 证伪，或者与基本面无关（情绪、资金面）。`,
    '3. 给结论：框架是否需要修订、改哪几处，还是只需继续观察。',
    '',
    '只在对话里讨论，不写文件；落盘等我说「可以落盘」，再走 sediment-framework。',
  ].join('\n');
}
```

`hitWhen` gives `10-08 04:20 盘后` (or `10-08 04:20` for the regular session), and the template puts a space on both sides, matching the spec's 「在 {时间 时段} 出现异动」.

In `earningsPrompt`, build the lines array as now and, when `it.anomaly` is set, insert after the first line:

```ts
const filed = it.anomaly
  ? [`财报前后出现异动：${hitWhen(it.anomaly)} 相对基准收盘 ${it.anomaly.ref} ${moveText(it.anomaly)}（约平时波动的 ${it.anomaly.mult.toFixed(1)} 倍），请一并解释。`]
  : [];
```

and splice `...filed` right after the first line of both templates.

`app/web/src/alerts/AlertRow.tsx`:

```tsx
/** One alert row, shared by the earnings and anomaly pages: open the doc, research, dismiss or restore. */
export function AlertRow({ title, sub, done, pending, canRestore, onOpen, onResearch, onIgnore, onRestore }: {
  title: string;
  sub: string;
  done: string | null; // 已调研 / 已忽略, greyed
  pending: boolean; // shows ×
  canRestore: boolean; // shows 恢复
  onOpen: () => void;
  onResearch: () => void;
  onIgnore: () => void;
  onRestore: () => void;
}) {
  return (
    <div className={`cand-row ${done ? 'earn-done' : ''}`}>
      <button className="chat-row-main earn-main" onClick={onOpen}>
        <span className="chat-row-title">{title}</span>
        <span className="earn-sub">{sub}{done ? ` · ${done}` : ''}</span>
      </button>
      <button className="text-btn" onClick={onResearch}>调研</button>
      {pending && <button className="x-btn" aria-label={`忽略 ${title}`} onClick={onIgnore}>×</button>}
      {canRestore && <button className="text-btn" onClick={onRestore}>恢复</button>}
    </div>
  );
}
```

`EarningsPage.tsx` — replace the inline row with:

```tsx
  const row = (it: EarningsItem) => (
    <AlertRow key={it.ticker} title={label(it.ticker)}
      sub={`${it.period} · ${whenText(it)}${it.anomaly ? ` · 异动 ${moveText(it.anomaly)}${SESSION_CN[it.anomaly.session] ? ` ${SESSION_CN[it.anomaly.session]}` : ''}` : ''}`}
      done={doneTag(it)} pending={isPending(it)} canRestore={it.status === 'released' && it.ignored && !it.researched}
      onOpen={() => onOpenDoc(it.ticker)} onResearch={() => research(it)}
      onIgnore={() => onIgnore(it)} onRestore={() => onUnignore(it)} />
  );
```

`app/web/src/anomalies/AnomaliesPage.tsx`:

```tsx
import { AlertRow } from '../alerts/AlertRow';
import { doneTag } from '../alerts/model';
import { loadDraft, saveDraft } from '../chat/drafts';
import { anomalyPrompt, withPrompt } from '../earnings/prompts';
import { hitText, isPending } from './model';
import type { AnomalyItem, AnomalyList } from './types';

/** Pool tickers whose price moved far from its base close, newest event per ticker, each a tap from research. */
export function AnomaliesPage({ list, error, label, onBack, onResearch, onIgnore, onUnignore, onOpenDoc }: {
  list: AnomalyList | null;
  error: string | null; // the page's own request failed
  label: (ticker: string) => string;
  onBack: () => void;
  onResearch: (ticker: string) => void; // opens the ticker's new 讨论 page, whose draft was just written
  onIgnore: (it: AnomalyItem) => void;
  onUnignore: (it: AnomalyItem) => void;
  onOpenDoc: (ticker: string) => void;
}) {
  const research = (it: AnomalyItem) => {
    const key = `new/${it.ticker}`; // ChatPage's draft key for a new 讨论 chat on this ticker
    saveDraft(key, withPrompt(loadDraft(key), anomalyPrompt(it)));
    onResearch(it.ticker);
  };
  return (
    <div className="chats">
      <header className="bar bar-top">
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <div className="chat-title-main">异动</div>
        </div>
      </header>
      <main className="chats-body">
        {error && <div className="banner-error">{error}</div>}
        {list?.error && <div className="banner-error">{list.error}；登录过期时在终端运行 longbridge auth login</div>}
        {list?.items.map((it) => (
          <AlertRow key={it.ticker} title={label(it.ticker)} sub={hitText(it)} done={doneTag(it)}
            pending={isPending(it)} canRestore={it.ignored && !it.researched}
            onOpen={() => onOpenDoc(it.ticker)} onResearch={() => research(it)}
            onIgnore={() => onIgnore(it)} onRestore={() => onUnignore(it)} />
        ))}
        {list && list.items.length === 0 && <p className="empty">近 7 天没有异动</p>}
        {!list && !error && <p className="empty">加载中…</p>}
      </main>
    </div>
  );
}
```

`route.ts`: add `| { page: 'anomalies' }`, `if (hash === '#/anomalies') return { page: 'anomalies' };` beside the earnings line, and `export function anomaliesHash(): string { return '#/anomalies'; }`.

`HomePage.tsx`: new props `anomaliesPending: number; onOpenAnomalies: () => void;`; import `{ menuLabel as anomalyMenuLabel } from '../anomalies/model'`; badge `{(earningsPending > 0 || anomaliesPending > 0) && <span className="badge badge-alert">!</span>}`; menu items gain, right after the 财报 row, `{ label: anomalyMenuLabel(anomaliesPending), onSelect: onOpenAnomalies },`. Update the badge comment to mention anomalies.

`App.tsx`:
- Rename `EARNINGS_POLL_MS` to `ALERT_POLL_MS` (5 minutes; the server's quote poll pace) and use it for earnings, anomalies and the home list.
- Anomaly state, mirroring earnings:

```tsx
  const [anomalies, setAnomalies] = useState<AnomalyList | null>(null);
  const [anomaliesError, setAnomaliesError] = useState<string | null>(null);
  const loadAnomalies = useCallback(() => {
    fetchAnomalies().then((l) => { setAnomalies(l); setAnomaliesError(null); },
      (e: unknown) => setAnomaliesError(String(e instanceof Error ? e.message : e)));
  }, []);
  // Anomalies are judged on the server's 5-minute quote batches; poll at the same pace.
  useEffect(() => {
    if (route.page !== 'home' && route.page !== 'anomalies') return;
    loadAnomalies();
    const timer = window.setInterval(loadAnomalies, ALERT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [route.page, loadAnomalies]);
  const dismissAnomaly = (p: Promise<unknown>) =>
    p.then(loadAnomalies, (e: unknown) => setAnomaliesError(String(e instanceof Error ? e.message : e)));
```

- Home list polling (after `useEffect(() => load(false), [load]);`):

```tsx
  // The server refetches quotes every 5 minutes; re-read the list at that pace while it is on screen.
  useEffect(() => {
    if (route.page !== 'home') return;
    const timer = window.setInterval(() => load(false), ALERT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [route.page, load]);
```

- Route render before the doc branch:

```tsx
  if (route.page === 'anomalies') {
    return (
      <AnomaliesPage list={anomalies} error={anomaliesError} label={(t) => labels.get(t) ?? t} onBack={goBack}
        onResearch={(t) => { window.location.hash = newChatHash(t); }}
        onIgnore={(it) => dismissAnomaly(ignoreAnomaly(it.ticker, it.ref))}
        onUnignore={(it) => dismissAnomaly(unignoreAnomaly(it.ticker, it.ref))}
        onOpenDoc={(t) => { window.location.hash = docHash(t); }} />
    );
  }
```

- `HomePage` gets `anomaliesPending={anomalyPendingCount(anomalies)} onOpenAnomalies={() => openFromHome(anomaliesHash())}` (import `pendingCount as anomalyPendingCount`).

- [ ] **Step 4: Run tests and the build**

Run: `npm --prefix app/web test -- --run && npm --prefix app/web run build`
Expected: all vitest files pass; `tsc --noEmit` clean; vite build succeeds.

- [ ] **Step 5: Commit**

```bash
git add app/web/src
git commit -m "feat(app): anomaly page behind the ⋯ menu; ! for earnings or anomalies; lists re-read every 5 minutes"
```

---

### Task 7: Live smoke against real longbridge (no commit)

**Files:** none committed; scratch under `./_local/tmp/` in the worktree.

- [ ] **Step 1: Baselines for the real pool**

Run (from `app/server`, `PYTHONPATH=.`):

```bash
PYTHONPATH=. uv run python - <<'EOF'
from pathlib import Path
from lynch_reader.anomalies import BaselineCache
from lynch_reader.catalog import FRAMEWORKS_DIR, quote_pool
pool = quote_pool(FRAMEWORKS_DIR)
got, day, err = BaselineCache(Path("../../_local/tmp/smoke-baseline.json")).get(pool)
print(len(pool), "tickers;", sum(v is not None for v in got.values()), "baselines; day", day, "error", err)
print({t: (round(got[t].sigma, 4), got[t].volume) for t in ("VST.US", "WOLF.US") if got.get(t)})
EOF
```

Expected: ~94 tickers, nearly all with baselines; VST sigma ≈ 0.03, WOLF ≈ 0.07; error None.

- [ ] **Step 2: Judge one real batch**

```bash
PYTHONPATH=. uv run python - <<'EOF'
import time
from pathlib import Path
from lynch_reader.anomalies import Anomalies, AnomalyStore, BaselineCache
from lynch_reader.catalog import FRAMEWORKS_DIR, QuoteCache, quote_pool
from lynch_reader.earnings import CalendarCache
tmp = Path("../../_local/tmp")
pool = quote_pool(FRAMEWORKS_DIR)
a = Anomalies(AnomalyStore(tmp / "smoke-events.json"), BaselineCache(tmp / "smoke-baseline.json"),
              CalendarCache(tmp / "smoke-calendar.json"))
q = QuoteCache()
q.subscribe(lambda b: a.observe(b, pool))
t = time.time(); q.read(pool); print("fetch+judge", round(time.time() - t, 2), "s")
print(a.snapshot(pool, [], None))
EOF
```

Expected: under ~2 s; a snapshot dict (likely empty items on a quiet weekend). Ledger the output.

---
