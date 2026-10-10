# App Earnings Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The phone app shows which pool tickers just released or are about to release earnings, flags unhandled releases on the home ⋯ button, and opens a research chat pre-filled with a built-in prompt; the Feishu earnings notifier is retired.

**Architecture:** `eval/lib/earnings_lb.py` fetches every known release from `longbridge finance-calendar` (10 symbols per call). The app server (`lynch_reader/earnings.py`) caches that calendar on disk, refetches in full at most once per Beijing day (new tickers alone in between), judges released / upcoming from each release's session slot and the clock, and joins in ignores (its own file) and research (ticker chats started after the release). The web app polls `GET /api/earnings`, badges ⋯ with「!」, and renders an earnings page whose 调研 button writes the prompt into the new chat's draft.

**Tech Stack:** Python 3.12 (unittest for `eval/lib`, pytest + Starlette TestClient for `app/server`), React 19 + TypeScript + vitest (`app/web`), longbridge CLI.

**Spec:** `docs/superpowers/specs/2026-10-11-app-earnings-design.md`

## Global Constraints

- Only `longbridge finance-calendar report` is called; never `financial-report`.
- At most one full calendar fetch per Beijing (Asia/Shanghai) day; tickers never fetched before are fetched alone; tickers no longer in the pool are filtered, not fetched; page views make no calls.
- On any fetch failure: keep old events, do not advance the fetched day, retry no sooner than 30 minutes.
- Release moment: `before` → local 09:30; `during` / `after` / unknown → local close (A 15:00, HK 16:00, US 16:00 America/New_York). Time zones from `lynch_reader.market_hours.MARKET_TZ`.
- Released: release moment ≤ now and (local today − release date) ≤ 7 days. Upcoming: release moment > now and (release date − local today) ≤ 3 days. One row per ticker: latest released, else earliest upcoming.
- Pending = released ∧ not researched ∧ not ignored. Only pending drives「!」and the menu count.
- Researched = some `kind == "ticker"` chat for that ticker (archived included) has `created_at` ≥ the release moment.
- Ignore key = (ticker, release date); stored in `output/earnings/ignored.json`; keys older than the released window are pruned on write.
- Archived frameworks (`output/archive/frameworks.json`) are left out of the earnings pool.
- Server-written files go under `output/earnings/` only, written atomically (tmp + `os.replace`).
- User-facing text is Simplified Chinese; identifiers, comments, commit messages English.
- Never stage `_local/**`. Only stage the files a task names.

## Review Focus

1. **Calendar returns a ticker under its zero-stripped HK code (`700.HK` for `0700.HK`)** — the row must still appear under `0700.HK`. Pinned in Task 1 (`test_hk_codes_map_back_to_framework_tickers`).
2. **longbridge is logged out or missing on the first open of the day** — the page loads with a banner and yesterday's data, and the next request inside 30 minutes makes no call. Pinned in Task 3 (`test_failure_keeps_old_events_and_backs_off`).
3. **A chat opened just before a release (e.g. pre-earnings prep)** — must not mark the release researched. Pinned in Task 3 (`test_chat_before_release_is_not_research`).
4. **Same ticker has two calendar entries a day apart (Q2 and 半年报)** — one row only. Pinned in Task 2 (`test_two_entries_make_one_row`).
5. **Unsent draft already in the ticker's new-chat page** — tapping 调研 keeps it and appends the prompt. Pinned in Task 5 (`withPrompt` tests).

---

## File Structure

| File | Responsibility |
|------|----------------|
| `eval/lib/earnings_lb.py` (rewrite) | longbridge calendar → `CalendarEvent`s per framework ticker; batch failures reported, never raised |
| `eval/lib/test_earnings_lb.py` (new) | parsing, HK code mapping, batching, failures |
| `eval/lib/earnings_available.py`, `eval/lib/test_earnings_available.py`, `scripts/notify_earnings_available.py` | deleted |
| `scripts/install-daily-cron.sh` | drop earnings lines; strip retired markers from crontab |
| `app/server/lynch_reader/earnings.py` (new) | release moment, released/upcoming judgment, calendar cache, ignore store, response assembly |
| `app/server/tests/test_earnings.py` (new) | judgment, cache, ignores, research |
| `app/server/lynch_reader/chat/service.py` | `ticker_chat_starts()` |
| `app/server/lynch_reader/web.py` | `/api/earnings` routes, wiring in `main` |
| `app/server/tests/test_web.py`, `tests/test_chat_service.py` | route + chat-start tests |
| `AGENTS.md` | document earnings in「app 对话」|
| `app/web/src/earnings/types.ts`, `api.ts`, `model.ts`, `prompts.ts` (new) + tests | data shapes, HTTP, labels/counts, prompt text |
| `app/web/src/earnings/EarningsPage.tsx` (new) | the page |
| `app/web/src/route.ts`, `App.tsx`, `home/HomePage.tsx`, `styles.css` | route, polling, ⋯ badge + menu row, styles |

---

### Task 1: Calendar fetcher, and retire the Feishu earnings notifier

**Files:**
- Rewrite: `eval/lib/earnings_lb.py`
- Create: `eval/lib/test_earnings_lb.py`
- Delete: `eval/lib/earnings_available.py`, `eval/lib/test_earnings_available.py`, `scripts/notify_earnings_available.py`
- Modify: `scripts/install-daily-cron.sh`

**Interfaces:**
- Produces:
  - `CalendarEvent(date: str, slot: str, period: str)` — frozen dataclass; `date` ISO market-local, `slot` ∈ `before|during|after|unknown`.
  - `CalendarFetch(events: dict[str, list[CalendarEvent]], failed: list[str], error: str | None)` — every ticker of a successful batch has a key (maybe `[]`); `failed` are tickers of failed batches; `error` the first failure's message.
  - `fetch_calendar(tickers: list[str], start: date, end: date, *, run=_run_json, sleep=time.sleep) -> CalendarFetch` — never raises for CLI/network failures.
  - `parse_calendar(payload: Any) -> dict[str, list[CalendarEvent]]` keyed by normalized calendar symbol.
  - `normalize_symbol(ticker: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `eval/lib/test_earnings_lb.py`:

```python
"""Tests for the longbridge earnings-calendar fetcher (no CLI: the runner is faked)."""
from __future__ import annotations

import unittest
from datetime import date

from eval.lib.earnings_lb import CalendarEvent, fetch_calendar, normalize_symbol, parse_calendar


def info(symbol: str, local_date: str | None, market_time: str | None, content: str) -> dict:
    ext: dict = {"financial_report": {"fiscal_year": "2026", "market_time": market_time, "period": "3",
                                      "period_type": "qf"}}
    if local_date is not None:
        ext["local_date"] = local_date
    return {"symbol": symbol, "content": content, "ext": ext}


def payload(*infos: dict) -> dict:
    # The CLI groups infos by day; grouping does not matter to the parser.
    return {"list": [{"infos": list(infos)}]}


class NormalizeTests(unittest.TestCase):
    def test_hk_strips_leading_zeros_only(self) -> None:
        self.assertEqual(normalize_symbol("0700.HK"), "700.HK")
        self.assertEqual(normalize_symbol("700.HK"), "700.HK")
        self.assertEqual(normalize_symbol("NVDA.US"), "NVDA.US")
        self.assertEqual(normalize_symbol("600036.SH"), "600036.SH")


class ParseTests(unittest.TestCase):
    def test_slot_period_and_date(self) -> None:
        got = parse_calendar(payload(
            info("TSM.US", "2026-10-14", "U", "2026 财年第 3 季度业绩披露"),
            info("1088.HK", "2026-10-23", "before", "2026 财年第 3 季度业绩披露"),
            info("NOK.US", "2026-10-22", "during", "2026 财年第 3 季度业绩披露"),
            info("MSFT.US", "2026-10-28", "after", "2027 财年第 1 季度业绩披露"),
        ))
        self.assertEqual(got["TSM.US"], [CalendarEvent("2026-10-14", "unknown", "2026 财年第 3 季度")])
        self.assertEqual(got["1088.HK"][0].slot, "before")
        self.assertEqual(got["NOK.US"][0].slot, "during")
        self.assertEqual(got["MSFT.US"][0], CalendarEvent("2026-10-28", "after", "2027 财年第 1 季度"))

    def test_entries_without_iso_date_are_dropped(self) -> None:
        got = parse_calendar(payload(
            info("AAA.US", None, "after", "x"),
            info("BBB.US", "10/14", "after", "x"),
            info("CCC.US", "2026-13-01", "after", "x"),
        ))
        self.assertEqual(got, {})

    def test_garbage_payload_is_empty(self) -> None:
        self.assertEqual(parse_calendar(None), {})
        self.assertEqual(parse_calendar({"list": "nope"}), {})


class FakeRun:
    """Stands in for `_run_json`: answers by the --symbol flags, or raises for listed symbols."""

    def __init__(self, infos: list[dict], fail: set[str] = frozenset()) -> None:
        self.infos, self.fail, self.calls = infos, fail, []

    def __call__(self, cmd: list[str]) -> dict:
        symbols = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--symbol"]
        self.calls.append(symbols)
        if self.fail & set(symbols):
            raise RuntimeError("longbridge failed (exit 1): token expired")
        wanted = {normalize_symbol(s) for s in symbols}
        return payload(*[i for i in self.infos if normalize_symbol(i["symbol"]) in wanted])


class FetchTests(unittest.TestCase):
    START, END = date(2026, 10, 4), date(2026, 11, 10)

    def test_batches_of_ten_with_every_ticker_keyed(self) -> None:
        tickers = [f"T{i:02d}.US" for i in range(23)]
        run = FakeRun([info("T05.US", "2026-10-20", "after", "2026 财年第 3 季度业绩披露")])
        got = fetch_calendar(tickers, self.START, self.END, run=run, sleep=lambda s: None)
        self.assertEqual([len(c) for c in run.calls], [10, 10, 3])
        self.assertEqual(set(got.events), set(tickers))
        self.assertEqual(got.events["T05.US"], [CalendarEvent("2026-10-20", "after", "2026 财年第 3 季度")])
        self.assertEqual(got.events["T00.US"], [])
        self.assertEqual((got.failed, got.error), ([], None))

    def test_hk_codes_map_back_to_framework_tickers(self) -> None:
        run = FakeRun([info("700.HK", "2026-11-12", "after", "2026 财年第 3 季度业绩披露")])
        got = fetch_calendar(["0700.HK"], self.START, self.END, run=run, sleep=lambda s: None)
        self.assertEqual(got.events["0700.HK"][0].date, "2026-11-12")

    def test_failed_batch_is_reported_and_others_kept(self) -> None:
        tickers = [f"T{i:02d}.US" for i in range(12)]
        run = FakeRun([info("T11.US", "2026-10-20", "after", "x")], fail={"T03.US"})
        got = fetch_calendar(tickers, self.START, self.END, run=run, sleep=lambda s: None)
        self.assertEqual(got.failed, tickers[:10])
        self.assertIn("token expired", got.error)
        self.assertEqual(set(got.events), {"T10.US", "T11.US"})

    def test_passes_the_window_to_the_cli(self) -> None:
        seen: list[list[str]] = []
        fetch_calendar(["AAA.US"], self.START, self.END, run=lambda c: seen.append(c) or payload(),
                       sleep=lambda s: None)
        cmd = seen[0]
        self.assertEqual(cmd[:3], ["longbridge", "finance-calendar", "report"])
        self.assertEqual(cmd[cmd.index("--start") + 1], "2026-10-04")
        self.assertEqual(cmd[cmd.index("--end") + 1], "2026-11-10")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/neko/plat1ko/lynch && python3 -m unittest eval.lib.test_earnings_lb -v`
Expected: FAIL / ERROR — `ImportError: cannot import name 'CalendarEvent'`.

- [ ] **Step 3: Rewrite `eval/lib/earnings_lb.py`**

Replace the whole file with:

```python
"""Longbridge finance-calendar fetcher: every known earnings release of the given tickers.

The app's earnings list judges released / upcoming from these events and the clock
(app/server/lynch_reader/earnings.py); nothing here reads report periods.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

_CAL_BATCH = 10  # --symbol flags per call
_CAL_COUNT = 100  # the CLI's maximum rows per call
_BATCH_PAUSE_S = 0.5  # back-to-back calls trip Longbridge's per-second cap (code 429002)
_HTTP_TIMEOUT_S = 60.0
_SLOTS = ("before", "during", "after")  # anything else ("U", missing) is unknown
_PERIOD_SUFFIX = "业绩披露"


@dataclass(frozen=True)
class CalendarEvent:
    date: str  # market-local ISO date of the release
    slot: str  # before | during | after | unknown
    period: str  # the calendar's own wording, e.g. "2026 财年第 3 季度"


@dataclass
class CalendarFetch:
    events: dict[str, list[CalendarEvent]] = field(default_factory=dict)  # every ticker of a batch that answered
    failed: list[str] = field(default_factory=list)  # tickers of batches that did not
    error: str | None = None  # the first failure, for the page's banner


def normalize_symbol(ticker: str) -> str:
    """Align framework tickers with Longbridge calendar symbols (0700.HK → 700.HK)."""
    if "." not in ticker:
        return ticker
    code, mkt = ticker.rsplit(".", 1)
    if mkt == "HK" and code.isdigit():
        return f"{int(code)}.{mkt}"
    return ticker


def _parse_json_stdout(raw: str) -> Any:
    """longbridge may append update notices after the JSON payload."""
    text = (raw or "").strip()
    if not text:
        raise RuntimeError("longbridge returned empty stdout")
    start = text.find("{")
    if start < 0:
        start = text.find("[")
    if start < 0:
        raise RuntimeError(f"longbridge stdout has no JSON: {text[:200]!r}")
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    return data


def _run_json(cmd: list[str], *, timeout_s: float = _HTTP_TIMEOUT_S) -> Any:
    if not shutil.which("longbridge"):
        raise RuntimeError("longbridge CLI not found on PATH")
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(
            f"longbridge failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )
    return _parse_json_stdout(proc.stdout)


def parse_calendar(payload: Any) -> dict[str, list[CalendarEvent]]:
    """Events keyed by normalized calendar symbol; an entry without an ISO local date is dropped."""
    out: dict[str, list[CalendarEvent]] = {}
    days = payload.get("list") if isinstance(payload, dict) else None
    for day in days if isinstance(days, list) else []:
        for info in (day.get("infos") or []) if isinstance(day, dict) else []:
            if not isinstance(info, dict):
                continue
            sym = str(info.get("symbol") or "").strip()
            event = _event(info)
            if sym and event is not None:
                out.setdefault(normalize_symbol(sym), []).append(event)
    return out


def _event(info: dict[str, Any]) -> CalendarEvent | None:
    ext = info.get("ext") if isinstance(info.get("ext"), dict) else {}
    raw = str(ext.get("local_date") or "").strip()
    try:
        day = date.fromisoformat(raw[:10])
    except ValueError:  # missing, or the widget's MM/DD form
        return None
    report = ext.get("financial_report") if isinstance(ext.get("financial_report"), dict) else {}
    slot = report.get("market_time")
    period = str(info.get("content") or "").strip().removesuffix(_PERIOD_SUFFIX).strip()
    return CalendarEvent(day.isoformat(), slot if slot in _SLOTS else "unknown", period)


def fetch_calendar(
    tickers: list[str],
    start: date,
    end: date,
    *,
    run: Callable[[list[str]], Any] = _run_json,
    sleep: Callable[[float], None] = time.sleep,
) -> CalendarFetch:
    """Every release from `start` on, keyed by framework ticker. A batch that fails is reported, not raised.

    `end` is passed along, but the CLI has been seen to return later releases too; callers must not rely on it.
    """
    out = CalendarFetch()
    for i in range(0, len(tickers), _CAL_BATCH):
        batch = tickers[i: i + _CAL_BATCH]
        if i:
            sleep(_BATCH_PAUSE_S)
        cmd = ["longbridge", "finance-calendar", "report", "--format", "json",
               "--start", start.isoformat(), "--end", end.isoformat(), "--count", str(_CAL_COUNT)]
        for t in batch:
            cmd += ["--symbol", t]
        try:
            by_symbol = parse_calendar(run(cmd))
        except Exception as e:  # noqa: BLE001 — external CLI: missing, logged out, rate-limited, bad JSON
            log.warning("finance-calendar batch failed %s: %s", batch, e)
            out.failed += batch
            out.error = out.error or str(e)
            continue
        if sum(len(v) for v in by_symbol.values()) >= _CAL_COUNT:
            log.warning("finance-calendar batch hit the row cap; later releases may be missing: %s", batch)
        for t in batch:
            out.events[t] = by_symbol.get(normalize_symbol(t), [])
    return out
```

- [ ] **Step 4: Delete the Feishu notifier and its logic**

```bash
cd /home/neko/plat1ko/lynch
git rm -q eval/lib/earnings_available.py eval/lib/test_earnings_available.py scripts/notify_earnings_available.py
```

- [ ] **Step 5: Run the eval/lib tests**

Run: `cd /home/neko/plat1ko/lynch && python3 -m unittest discover -s eval/lib -t . -p 'test_*.py'`
Expected: all pass, including the new `test_earnings_lb` tests; nothing imports the deleted module (`git grep -n earnings_available` prints nothing but `_local` hits, which are not tracked).

- [ ] **Step 6: Update `scripts/install-daily-cron.sh`**

Replace the whole file with:

```bash
#!/usr/bin/env bash
# Install/update lynch user crontab lines (Asia/Shanghai wall clock).
# User crontab — no sudo. Idempotent via markers; re-run updates PATH lines.
#
#   lynch-daily-bands-am       weekdays 09:03  — morning zone digest
#   lynch-daily-bands          weekdays 16:13  — afternoon zone digest
#
# Retired markers are removed from the crontab on every run:
#   lynch-earnings-ha / lynch-earnings-us — Feishu earnings notifier, replaced by the app's earnings page
set -euo pipefail

REPO="/home/neko/plat1ko/lynch"
PY="${PYTHON:-/home/neko/miniconda3/bin/python3}"
CRON_PATH="/home/neko/.local/bin:/home/neko/miniconda3/bin:/usr/bin:/bin"
LOG_DIR="${REPO}/_local/tmp"

MARKER_BANDS_AM="# lynch-daily-bands-am"
MARKER_BANDS="# lynch-daily-bands"
RETIRED_MARKERS=("# lynch-earnings-ha" "# lynch-earnings-us")

LINE_BANDS_AM="3 9 * * 1-5 cd ${REPO} && PATH=${CRON_PATH} ${PY} scripts/daily_bands.py >> ${LOG_DIR}/daily_bands.log 2>&1 ${MARKER_BANDS_AM}"
LINE_BANDS="13 16 * * 1-5 cd ${REPO} && PATH=${CRON_PATH} ${PY} scripts/daily_bands.py >> ${LOG_DIR}/daily_bands.log 2>&1 ${MARKER_BANDS}"

mkdir -p "${LOG_DIR}"
echo "=== proposed crontab lines ==="
echo "${LINE_BANDS_AM}"
echo "${LINE_BANDS}"
echo

EXISTING="$(crontab -l 2>/dev/null || true)"
for marker in "${MARKER_BANDS_AM}" "${MARKER_BANDS}"; do
  if echo "${EXISTING}" | grep -F "${marker}" >/dev/null; then
    EXISTING="$(echo "${EXISTING}" | grep -Fv "${marker}")"
    echo "Will update: ${marker}"
  else
    echo "Will install: ${marker}"
  fi
done
for marker in "${RETIRED_MARKERS[@]}"; do
  if echo "${EXISTING}" | grep -F "${marker}" >/dev/null; then
    EXISTING="$(echo "${EXISTING}" | grep -Fv "${marker}")"
    echo "Will remove (retired): ${marker}"
  fi
done

{
  printf '%s\n' "${EXISTING}"
  echo "${LINE_BANDS_AM}"
  echo "${LINE_BANDS}"
} | sed '/./,$!d' | crontab -

echo "Installed. Verify with:"
echo "  crontab -l | grep lynch-"
```

- [ ] **Step 7: Apply it to the user crontab and verify**

```bash
cd /home/neko/plat1ko/lynch
crontab -l > _local/tmp/crontab.before-earnings-retire   # backup, never staged
bash scripts/install-daily-cron.sh
crontab -l | grep lynch-
diff <(grep -v lynch- _local/tmp/crontab.before-earnings-retire) <(crontab -l | grep -v lynch-) && echo "non-lynch lines unchanged"
```
Expected: exactly two `lynch-daily-bands*` lines remain; no `lynch-earnings-*`; "non-lynch lines unchanged".

- [ ] **Step 8: Commit**

```bash
cd /home/neko/plat1ko/lynch
git add eval/lib/earnings_lb.py eval/lib/test_earnings_lb.py scripts/install-daily-cron.sh
git commit -m "feat(eval): earnings calendar fetcher returns every release; retire the Feishu earnings notifier"
```
(The deletions were staged by `git rm` in Step 4.)

---

### Task 2: Release moment and released / upcoming judgment

**Files:**
- Create: `app/server/lynch_reader/earnings.py`
- Create: `app/server/tests/test_earnings.py`

**Interfaces:**
- Consumes: `CalendarEvent` from Task 1 (`eval.lib.earnings_lb`); `MARKET_TZ` from `lynch_reader.market_hours`; `market_bucket` from `eval.lib.frameworks`.
- Produces (in `lynch_reader.earnings`):
  - `RELEASED_DAYS = 7`, `UPCOMING_DAYS = 3`
  - `release_at(ticker: str, ev: CalendarEvent) -> datetime | None` (aware; None for an unknown market)
  - `Row(ticker: str, status: str, event: CalendarEvent, release_at: datetime, day_offset: int)` frozen dataclass; `status` ∈ `released|upcoming`
  - `judge(ticker: str, events: list[CalendarEvent], now: datetime) -> Row | None`

- [ ] **Step 1: Write the failing tests**

Create `app/server/tests/test_earnings.py`:

```python
from __future__ import annotations

from datetime import datetime

import pytest

from lynch_reader.earnings import judge, release_at
from lynch_reader.market_hours import MARKET_TZ
from eval.lib.earnings_lb import CalendarEvent  # after lynch_reader, which puts the repo root on sys.path

# 2026-10-14 is a Wednesday.


def ev(day: str, slot: str = "after", period: str = "2026 财年第 3 季度") -> CalendarEvent:
    return CalendarEvent(day, slot, period)


def at(market: str, month: int, day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=MARKET_TZ[market])


@pytest.mark.parametrize("ticker, slot, local", [
    ("TSM.US", "before", (9, 30)),
    ("TSM.US", "during", (16, 0)),
    ("TSM.US", "after", (16, 0)),
    ("TSM.US", "unknown", (16, 0)),
    ("0700.HK", "before", (9, 30)),
    ("0700.HK", "after", (16, 0)),
    ("600036.SH", "before", (9, 30)),
    ("600036.SH", "after", (15, 0)),
    ("000933.SZ", "during", (15, 0)),
])
def test_release_moment_by_slot_and_market(ticker, slot, local):
    market = {"US": "US", "HK": "HK", "SH": "A", "SZ": "A"}[ticker.rsplit(".", 1)[1]]
    assert release_at(ticker, ev("2026-10-14", slot)) == at(market, 10, 14, *local)


def test_unknown_market_has_no_moment():
    assert release_at("D05.SG", ev("2026-10-14")) is None
    assert judge("D05.SG", [ev("2026-10-14")], at("US", 10, 14, 12)) is None


@pytest.mark.parametrize("slot, before_flip, after_flip", [
    ("before", (9, 29), (9, 30)),
    ("after", (15, 59), (16, 0)),
    ("during", (15, 59), (16, 0)),
    ("unknown", (15, 59), (16, 0)),
])
def test_flips_to_released_at_the_moment(slot, before_flip, after_flip):
    events = [ev("2026-10-14", slot)]
    assert judge("TSM.US", events, at("US", 10, 14, *before_flip)).status == "upcoming"
    assert judge("TSM.US", events, at("US", 10, 14, *after_flip)).status == "released"


def test_a_share_after_close_releases_at_15_beijing():
    events = [ev("2026-10-20", "after")]
    assert judge("300750.SZ", events, at("A", 10, 20, 14, 59)).status == "upcoming"
    assert judge("300750.SZ", events, at("A", 10, 20, 15, 0)).status == "released"


def test_judged_in_the_markets_own_day():
    # 2026-10-15 01:00 Beijing is still 10-14 13:00 in New York: TSM's 10-14 after-close release is today, upcoming.
    row = judge("TSM.US", [ev("2026-10-14", "after")], at("A", 10, 15, 1))
    assert (row.status, row.day_offset) == ("upcoming", 0)


def test_upcoming_window_is_three_days():
    now = at("US", 10, 11, 12)
    assert judge("TSM.US", [ev("2026-10-14")], now).day_offset == 3
    assert judge("TSM.US", [ev("2026-10-15")], now) is None


def test_released_window_is_seven_days():
    events = [ev("2026-10-14", "after")]
    assert judge("TSM.US", events, at("US", 10, 21, 23)).day_offset == -7
    assert judge("TSM.US", events, at("US", 10, 22, 0)) is None


def test_two_entries_make_one_row():
    # The calendar lists a quarter and the half-year report a day apart.
    events = [ev("2026-07-27", "after", "2026 财年第 2 季度"), ev("2026-07-28", "after", "2026 财年半年报")]
    row = judge("000933.SZ", events, at("A", 7, 28, 10))
    assert (row.status, row.event.period) == ("released", "2026 财年第 2 季度")
    row = judge("000933.SZ", events, at("A", 7, 28, 16))
    assert (row.status, row.event.period) == ("released", "2026 财年半年报")


def test_released_wins_over_upcoming_and_nearest_upcoming_wins():
    assert judge("TSM.US", [ev("2026-10-14"), ev("2026-10-16")], at("US", 10, 14, 17)).event.date == "2026-10-14"
    assert judge("TSM.US", [ev("2026-10-17"), ev("2026-10-15")], at("US", 10, 14, 12)).event.date == "2026-10-15"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/neko/plat1ko/lynch/app/server && uv run pytest tests/test_earnings.py -q`
Expected: ERROR — `ModuleNotFoundError: No module named 'lynch_reader.earnings'`.

- [ ] **Step 3: Write `app/server/lynch_reader/earnings.py` (judgment part)**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/neko/plat1ko/lynch/app/server && uv run pytest tests/test_earnings.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /home/neko/plat1ko/lynch
git add app/server/lynch_reader/earnings.py app/server/tests/test_earnings.py
git commit -m "feat(app): judge released and upcoming earnings from the session slot and the market clock"
```

---

### Task 3: Calendar cache, ignore store, research, and the response

**Files:**
- Modify: `app/server/lynch_reader/earnings.py`
- Modify: `app/server/tests/test_earnings.py`

**Interfaces:**
- Consumes: `fetch_calendar`, `CalendarFetch`, `CalendarEvent` (Task 1); `judge`, `Row`, `RELEASED_DAYS` (Task 2).
- Produces (in `lynch_reader.earnings`):
  - `EARNINGS_DIR = REPO_ROOT / "output" / "earnings"`, `CALENDAR_FILE`, `IGNORED_FILE`, `RETRY_S = 1800`
  - `CalendarCache(path: Path, fetch: Callable[[list[str], date, date], CalendarFetch] = fetch_calendar, wall: Callable[[], float] = time.time)` with `.get(pool: list[str]) -> tuple[dict[str, list[CalendarEvent]], str | None, str | None]` returning (events of pool tickers, fetched Beijing day, error)
  - `IgnoreStore(path: Path, wall=time.time)` with `.keys() -> set[tuple[str, str]]`, `.ignore(ticker, day)`, `.unignore(ticker, day)`
  - `Earnings(calendar: CalendarCache, ignored: IgnoreStore, wall=time.time)` with `.snapshot(pool: list[str], chat_starts: list[tuple[str, float]]) -> dict` — the `GET /api/earnings` body

- [ ] **Step 1: Write the failing tests**

Append to `app/server/tests/test_earnings.py`:

```python
import json

from eval.lib.earnings_lb import CalendarFetch
from lynch_reader.earnings import RETRY_S, CalendarCache, Earnings, IgnoreStore

BJT = MARKET_TZ["A"]


class Clock:
    def __init__(self, t: datetime) -> None:
        self.now = t.timestamp()

    def __call__(self) -> float:
        return self.now

    def set(self, t: datetime) -> None:
        self.now = t.timestamp()


class FakeCalendar:
    """Stands in for `fetch_calendar`: fixed events, a call log, and optional failing tickers or a raise."""

    def __init__(self, events: dict[str, list[CalendarEvent]]) -> None:
        self.events, self.calls, self.fail, self.error = events, [], set(), None

    def __call__(self, tickers, start, end) -> CalendarFetch:
        self.calls.append((list(tickers), start, end))
        if self.error is not None:
            raise self.error
        failed = [t for t in tickers if t in self.fail]
        return CalendarFetch({t: self.events.get(t, []) for t in tickers if t not in failed}, failed,
                             "longbridge failed (exit 1): token expired" if failed else None)


@pytest.fixture
def cal():
    return FakeCalendar({"TSM.US": [ev("2026-10-14", "after")], "0700.HK": [ev("2026-11-12", "after")]})


def test_one_full_fetch_per_beijing_day(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    events, day, error = cache.get(["TSM.US", "0700.HK"])
    assert (day, error) == ("2026-10-14", None)
    assert events["TSM.US"] == [ev("2026-10-14", "after")]
    _, start, end = cal.calls[0]
    assert (start.isoformat(), end.isoformat()) == ("2026-10-07", "2026-11-13")
    clk.set(at("A", 10, 14, 23, 59))
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 1
    clk.set(at("A", 10, 15, 0, 1))
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 2 and cal.calls[1][0] == ["TSM.US", "0700.HK"]


def test_new_ticker_is_fetched_alone_and_removed_one_filtered(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    cache.get(["TSM.US"])
    events, _, _ = cache.get(["TSM.US", "0700.HK"])
    assert cal.calls[1][0] == ["0700.HK"] and "0700.HK" in events
    events, _, _ = cache.get(["0700.HK"])
    assert len(cal.calls) == 2 and set(events) == {"0700.HK"}


def test_ticker_without_events_is_not_refetched(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    cache.get(["NOPE.US"])
    events, _, _ = cache.get(["NOPE.US"])
    assert events == {"NOPE.US": []} and len(cal.calls) == 1


def test_cache_survives_a_restart(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    events, day, _ = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    assert len(cal.calls) == 1 and day == "2026-10-14" and events["TSM.US"][0].slot == "after"


def test_corrupt_cache_file_starts_empty(tmp_path, cal):
    (tmp_path / "calendar.json").write_text("{not json", encoding="utf-8")
    clk = Clock(at("A", 10, 14, 8))
    events, _, _ = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    assert events["TSM.US"] and len(cal.calls) == 1


def test_failure_keeps_old_events_and_backs_off(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    cache.get(["TSM.US", "0700.HK"])
    clk.set(at("A", 10, 15, 8))
    cal.fail = {"TSM.US"}
    events, day, error = cache.get(["TSM.US", "0700.HK"])
    assert day == "2026-10-14" and "token expired" in error
    assert events["TSM.US"] == [ev("2026-10-14", "after")]  # yesterday's events kept
    clk.now += RETRY_S - 1
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 2  # inside the back-off: no call
    cal.fail = set()
    clk.now += 1
    events, day, error = cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 3 and (day, error) == ("2026-10-15", None)


def test_fetch_that_raises_is_a_failure_not_a_crash(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cal.error = RuntimeError("boom")
    events, day, error = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    assert (events, day) == ({"TSM.US": []}, None) and "boom" in error


def test_ignore_is_per_release_and_pruned(tmp_path):
    clk = Clock(at("A", 10, 14, 8))
    store = IgnoreStore(tmp_path / "ignored.json", wall=clk)
    store.ignore("TSM.US", "2026-10-14")
    store.ignore("TSM.US", "2026-10-14")
    assert store.keys() == {("TSM.US", "2026-10-14")}
    store.unignore("TSM.US", "2026-10-14")
    assert store.keys() == set()
    store.ignore("AAA.US", "2026-10-10")
    assert store.keys() == {("AAA.US", "2026-10-10")}  # still inside the window on 10-14
    clk.set(at("A", 10, 20, 8))
    store.ignore("BBB.US", "2026-10-20")  # writing prunes keys past the released window
    assert store.keys() == {("BBB.US", "2026-10-20")}
    assert json.loads((tmp_path / "ignored.json").read_text()) == [["BBB.US", "2026-10-20"]]


def snapshot(tmp_path, cal, now: datetime, chat_starts=(), ignore=()):
    clk = Clock(now)
    store = IgnoreStore(tmp_path / "ignored.json", wall=clk)
    for key in ignore:
        store.ignore(*key)
    e = Earnings(CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk), store, wall=clk)
    return e.snapshot(["TSM.US", "0700.HK"], list(chat_starts))


def test_snapshot_shape(tmp_path, cal):
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17))
    assert body["fetched_day"] == "2026-10-15" and body["error"] is None  # 17:00 New York is 05:00 next day Beijing
    assert body["items"] == [{
        "ticker": "TSM.US", "status": "released", "date": "2026-10-14", "day_offset": 0, "slot": "after",
        "release_at": "2026-10-14T16:00:00-04:00", "period": "2026 财年第 3 季度",
        "researched": False, "ignored": False,
    }]


def test_chat_before_release_is_not_research(tmp_path, cal):
    release = at("US", 10, 14, 16).timestamp()
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17), chat_starts=[("TSM.US", release - 60)])
    assert body["items"][0]["researched"] is False
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17), chat_starts=[("TSM.US", release), ("0700.HK", release)])
    assert body["items"][0]["researched"] is True


def test_ignored_release_is_flagged(tmp_path, cal):
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17), ignore=[("TSM.US", "2026-10-14")])
    assert body["items"][0]["ignored"] is True


def test_released_newest_first_then_upcoming_soonest_first(tmp_path):
    cal = FakeCalendar({
        "AAA.US": [ev("2026-10-12", "after")], "BBB.US": [ev("2026-10-13", "after")],
        "CCC.US": [ev("2026-10-16", "after")], "DDD.US": [ev("2026-10-15", "before")],
    })
    clk = Clock(at("US", 10, 14, 12))
    e = Earnings(CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk), IgnoreStore(tmp_path / "i.json", wall=clk),
                 wall=clk)
    items = e.snapshot(["AAA.US", "BBB.US", "CCC.US", "DDD.US"], [])["items"]
    assert [i["ticker"] for i in items] == ["BBB.US", "AAA.US", "DDD.US", "CCC.US"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/neko/plat1ko/lynch/app/server && uv run pytest tests/test_earnings.py -q`
Expected: ERROR — `ImportError: cannot import name 'RETRY_S'`.

- [ ] **Step 3: Implement the stateful part**

In `app/server/lynch_reader/earnings.py`, extend the imports to:

```python
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
```

(drop the `# noqa: F401` on the `REPO_ROOT` import: it is now used.) After `CLOSE`, add:

```python
EARNINGS_DIR = REPO_ROOT / "output" / "earnings"
CALENDAR_FILE = EARNINGS_DIR / "calendar.json"
IGNORED_FILE = EARNINGS_DIR / "ignored.json"
RETRY_S = 30 * 60  # after a failed fetch; the home page asks every 5 minutes
LOOKAHEAD_DAYS = 30  # passed as --end; the CLI returns later releases anyway
BJT = MARKET_TZ["A"]  # the fetch day is a Beijing day: the user's day

Fetch = Callable[[list[str], date, date], CalendarFetch]
```

Append to the end of the module:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/neko/plat1ko/lynch/app/server && uv run pytest tests/test_earnings.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /home/neko/plat1ko/lynch
git add app/server/lynch_reader/earnings.py app/server/tests/test_earnings.py
git commit -m "feat(app): earnings calendar cache fetched once a day, ignores per release, research from chat starts"
```

---

### Task 4: HTTP routes, chat starts, wiring, AGENTS.md

**Files:**
- Modify: `app/server/lynch_reader/chat/service.py` (add `ticker_chat_starts` in the `# ---- queries` block after `list`)
- Modify: `app/server/lynch_reader/web.py`
- Modify: `app/server/tests/test_web.py`, `app/server/tests/test_chat_service.py`
- Modify: `AGENTS.md` (「app 对话」section)

**Interfaces:**
- Consumes: `Earnings`, `CalendarCache`, `IgnoreStore`, `CALENDAR_FILE`, `IGNORED_FILE` (Task 3).
- Produces:
  - `ChatService.ticker_chat_starts() -> list[tuple[str, float]]`
  - `create_app(..., chat: ChatService | None = None, earnings: Earnings | None = None)`
  - Routes: `GET /api/earnings`; `POST /api/earnings/ignore` and `POST /api/earnings/unignore` with JSON body `{"ticker": str, "date": "YYYY-MM-DD"}` → 204; 400 `{"error": ...}` on bad input; 415 for non-JSON.

- [ ] **Step 1: Write the failing tests**

Append to `app/server/tests/test_chat_service.py`:

```python
def test_ticker_chat_starts_include_archived_and_skip_other_kinds(tmp_path):
    from lynch_reader.chat.index import ChatIndex
    from tests.chat_helpers import conv
    index = ChatIndex(tmp_path / "index.json")
    index.put(conv(id="a", ticker="AAA.US", created_at=10.0))
    index.put(conv(id="b", ticker="BBB.US", created_at=20.0, archived=True))
    index.put(conv(id="c", kind="research", ticker=None, created_at=30.0))
    index.put(conv(id="d", kind="discover", ticker=None, created_at=40.0))
    svc, _ = make_service(tmp_path)
    assert sorted(svc.ticker_chat_starts()) == [("AAA.US", 10.0), ("BBB.US", 20.0)]
```

In `app/server/tests/test_web.py`, change the `client` helper and add tests:

```python
from datetime import datetime

from eval.lib.earnings_lb import CalendarEvent, CalendarFetch
from lynch_reader.earnings import CalendarCache, Earnings, IgnoreStore
from lynch_reader.market_hours import MARKET_TZ


def fake_calendar(tickers, start, end):
    events = {"AAA.US": [CalendarEvent("2026-09-21", "before", "2026 财年第 3 季度")]}
    return CalendarFetch({t: events.get(t, []) for t in tickers}, [], None)


def client(repo, tmp_path, quotes, with_dist=True, calendar=fake_calendar):
    dist = tmp_path / "dist"
    if with_dist:
        dist.mkdir(exist_ok=True)
        (dist / "index.html").write_text("<!doctype html><title>lynch</title>", encoding="utf-8")
    devices, cookies = paired_devices(tmp_path)
    archive = FrameworkArchive(tmp_path / "archive" / "frameworks.json")
    wall = lambda: NOW  # noqa: E731 — Mon 2026-09-21 10:13 New York: AAA's before-open release is out
    earnings = Earnings(CalendarCache(tmp_path / "earnings" / "calendar.json", fetch=calendar, wall=wall),
                        IgnoreStore(tmp_path / "earnings" / "ignored.json", wall=wall), wall=wall)
    return TestClient(create_app(repo, dist, quotes, devices, archive, earnings=earnings), cookies=cookies)
```

and import `NOW` alongside the existing conftest imports: `from tests.conftest import NOW, FakeFetch, paired_devices`.

Note: the existing `client` used `dist.mkdir()`; `exist_ok=True` lets two clients share a `tmp_path` (the archive restart test already builds two clients with `with_dist=False`; keep its behaviour).

Tests:

```python
def test_earnings_lists_pool_tickers(repo, tmp_path, quotes):
    body = client(repo, tmp_path, quotes).get("/api/earnings").json()
    assert body["error"] is None and body["fetched_day"] == "2026-09-21"
    assert [(i["ticker"], i["status"]) for i in body["items"]] == [("AAA.US", "released")]


def test_earnings_skip_archived_tickers(repo, tmp_path, quotes):
    seen: list[list[str]] = []

    def calendar(tickers, start, end):
        seen.append(list(tickers))
        return fake_calendar(tickers, start, end)
    c = client(repo, tmp_path, quotes, calendar=calendar)
    c.post("/api/frameworks/AAA.US/archive", json={})
    assert c.get("/api/earnings").json()["items"] == []
    assert "AAA.US" not in seen[0]


def test_ignore_and_unignore(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    assert c.post("/api/earnings/ignore", json={"ticker": "AAA.US", "date": "2026-09-21"}).status_code == 204
    assert c.get("/api/earnings").json()["items"][0]["ignored"] is True
    assert c.post("/api/earnings/unignore", json={"ticker": "AAA.US", "date": "2026-09-21"}).status_code == 204
    assert c.get("/api/earnings").json()["items"][0]["ignored"] is False


def test_ignore_refuses_bad_input(repo, tmp_path, quotes):
    c = client(repo, tmp_path, quotes)
    for body in ({"ticker": "NOPE.US", "date": "2026-09-21"}, {"ticker": "AAA.US", "date": "09/21"},
                 {"ticker": "AAA.US"}, {"ticker": 1, "date": "2026-09-21"}):
        r = c.post("/api/earnings/ignore", json=body)
        assert r.status_code == 400 and r.json()["error"], body
    r = c.post("/api/earnings/ignore", content="ticker=AAA.US", headers={"content-type": "text/plain"})
    assert r.status_code == 415
    assert not (tmp_path / "earnings" / "ignored.json").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/neko/plat1ko/lynch/app/server && uv run pytest tests/test_web.py tests/test_chat_service.py -q`
Expected: FAIL — `create_app() got an unexpected keyword argument 'earnings'` and `'ChatService' object has no attribute 'ticker_chat_starts'`.

- [ ] **Step 3: Add `ChatService.ticker_chat_starts`**

In `app/server/lynch_reader/chat/service.py`, after `def list(self) -> dict:` … `return {...}`:

```python
    def ticker_chat_starts(self) -> list[tuple[str, float]]:
        """(ticker, created_at) of every 讨论 chat, archived ones included: the earnings list counts any chat on a
        ticker started after its release as research on it."""
        return [(c.ticker, c.created_at) for c in self._index.all() if c.kind == "ticker" and c.ticker]
```

- [ ] **Step 4: Add the routes and wiring in `web.py`**

Imports — add:

```python
from datetime import date

from lynch_reader.chat.routes import _body, _guarded, chat_lifespan, chat_routes
from lynch_reader.earnings import CALENDAR_FILE, IGNORED_FILE, CalendarCache, Earnings, IgnoreStore
from eval.lib.frameworks import list_framework_paths  # noqa: E402 — after catalog, which puts the repo root on sys.path
```

(the `chat.routes` line replaces the existing `from lynch_reader.chat.routes import _guarded, chat_lifespan, chat_routes`). Update the module docstring's last sentence to: "The server itself writes only output/chat/, output/candidates/, output/archive/ and output/earnings/."

Signature:

```python
def create_app(frameworks_dir: Path, web_dist: Path, quotes: QuoteCache, devices: DeviceStore,
               archive: FrameworkArchive, chat: ChatService | None = None,
               earnings: Earnings | None = None) -> Starlette:
```

Inside `create_app`, after `axis`:

```python
    def earnings_list(request: Request) -> Response:
        # Sync: the first request of a Beijing day waits on the longbridge calendar in the thread pool.
        hidden = archive.tickers()
        pool = [p.stem for p in list_framework_paths(frameworks_dir) if p.stem not in hidden]
        return JSONResponse(earnings.snapshot(pool, chat.ticker_chat_starts() if chat is not None else []))

    def set_ignored(ignored: bool):
        async def run(request: Request) -> Response:
            body = await _body(request)
            ticker, day = body.get("ticker"), body.get("date")
            if not isinstance(ticker, str) or not isinstance(day, str):
                raise Invalid("需要 ticker 和 date")
            try:
                date.fromisoformat(day)
            except ValueError:
                raise Invalid("date 须为 YYYY-MM-DD") from None
            if framework_path(ticker, frameworks_dir) is None:
                raise Invalid("没有这只票的框架")
            (earnings.ignored.ignore if ignored else earnings.ignored.unignore)(ticker, day)
            return Response(status_code=204)
        return _guarded(run)  # its CSRF guard: a cross-site form must not dismiss anything
```

After the `routes = [...]` list:

```python
    if earnings is not None:
        routes += [
            Route("/api/earnings", earnings_list),
            Route("/api/earnings/ignore", set_ignored(True), methods=["POST"]),
            Route("/api/earnings/unignore", set_ignored(False), methods=["POST"]),
        ]
```

In `main`, pass it:

```python
    uvicorn.run(create_app(FRAMEWORKS_DIR, WEB_DIST, QuoteCache(), DeviceStore(DEVICES_FILE),
                           FrameworkArchive(ARCHIVE_FILE), chat=chat,
                           earnings=Earnings(CalendarCache(CALENDAR_FILE), IgnoreStore(IGNORED_FILE))),
                host=args.host, port=args.port, timeout_graceful_shutdown=5)
```

- [ ] **Step 5: Run the whole server suite**

Run: `cd /home/neko/plat1ko/lynch/app/server && uv run pytest -q`
Expected: all pass (live grok test skips without `LYNCH_LIVE_GROK=1`).

- [ ] **Step 6: Document in `AGENTS.md`**

In `/home/neko/plat1ko/lynch/AGENTS.md`, section「app 对话」, insert after the「框架归档」bullet:

```markdown
- 财报提示：⋯ 菜单「财报」页列出框架池（不含已归档）里刚发布（发布日后 7 天内）和即将发布（3 天内）财报的票；待看（已出、没调研、没忽略）时 ⋯ 上亮「!」。日历只用 `longbridge finance-calendar`（`eval/lib/earnings_lb.py`），服务端每个北京日最多全量查一次、新加的框架单独查、失败 30 分钟内不重试，缓存与忽略清单在 `output/earnings/`（服务端唯一写者）；「已出」按发布时段与市场时钟判定（盘前按开盘，其余按收盘），不等财务数据入库。财报后开过该票讨论对话即算已调研。判定与缓存的唯一真源是 `app/server/lynch_reader/earnings.py`；调研提示词在 `app/web/src/earnings/prompts.ts`。飞书财报提醒已下线。
```

- [ ] **Step 7: Commit**

```bash
cd /home/neko/plat1ko/lynch
git add app/server/lynch_reader/chat/service.py app/server/lynch_reader/web.py \
  app/server/tests/test_web.py app/server/tests/test_chat_service.py AGENTS.md
git commit -m "feat(app): /api/earnings lists releases; ignore and unignore one release"
```

---

### Task 5: Web data layer — types, API, model, prompts, route

**Files:**
- Create: `app/web/src/earnings/types.ts`, `app/web/src/earnings/api.ts`, `app/web/src/earnings/model.ts`, `app/web/src/earnings/prompts.ts`
- Create: `app/web/src/earnings/model.test.ts`, `app/web/src/earnings/prompts.test.ts`
- Modify: `app/web/src/route.ts`, `app/web/src/route.test.ts`

**Interfaces:**
- Consumes: `get`, `post` from `src/api.ts`; the JSON of Task 4.
- Produces:
  - `EarningsItem`, `EarningsList`, `EarningsSlot` (types.ts)
  - `fetchEarnings(): Promise<EarningsList>`, `ignoreEarnings(ticker, date)`, `unignoreEarnings(ticker, date)` (api.ts)
  - `isPending(it)`, `pendingCount(list: EarningsList | null): number`, `menuLabel(n: number): string`, `whenText(it): string`, `doneTag(it): string | null`, `SLOT_CN` (model.ts)
  - `earningsPrompt(it: EarningsItem): string`, `withPrompt(draft: string, prompt: string): string` (prompts.ts)
  - Route `{ page: 'earnings' }`, `earningsHash(): string` (route.ts)

- [ ] **Step 1: Write the failing tests**

`app/web/src/earnings/model.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import { doneTag, isPending, menuLabel, pendingCount, whenText } from './model';
import type { EarningsItem } from './types';

const item = (o: Partial<EarningsItem> = {}): EarningsItem => ({
  ticker: 'TSM.US', status: 'released', date: '2026-10-14', day_offset: 0, slot: 'after',
  release_at: '2026-10-14T16:00:00-04:00', period: '2026 财年第 3 季度', researched: false, ignored: false, ...o,
});

describe('earnings model', () => {
  it('counts only released, unresearched, unignored rows', () => {
    const items = [item(), item({ researched: true }), item({ ignored: true }), item({ status: 'upcoming' })];
    expect(items.map(isPending)).toEqual([true, false, false, false]);
    expect(pendingCount({ fetched_day: '2026-10-14', error: null, items })).toBe(1);
    expect(pendingCount(null)).toBe(0);
  });
  it('labels the menu row', () => {
    expect(menuLabel(0)).toBe('财报');
    expect(menuLabel(2)).toBe('财报 · 2 待看');
  });
  it('says when, relative near today', () => {
    expect(whenText(item({ day_offset: 0, slot: 'after' }))).toBe('今天盘后');
    expect(whenText(item({ day_offset: 1, slot: 'before' }))).toBe('明天盘前');
    expect(whenText(item({ day_offset: -1, slot: 'during' }))).toBe('昨天盘中');
    expect(whenText(item({ day_offset: -3, date: '2026-10-11', slot: 'unknown' }))).toBe('10-11');
  });
  it('tags handled rows', () => {
    expect(doneTag(item({ researched: true, ignored: true }))).toBe('已调研');
    expect(doneTag(item({ ignored: true }))).toBe('已忽略');
    expect(doneTag(item())).toBeNull();
  });
});
```

`app/web/src/earnings/prompts.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import { earningsPrompt, withPrompt } from './prompts';
import type { EarningsItem } from './types';

const item = (o: Partial<EarningsItem> = {}): EarningsItem => ({
  ticker: 'TSM.US', status: 'released', date: '2026-10-14', day_offset: 0, slot: 'after',
  release_at: '2026-10-14T16:00:00-04:00', period: '2026 财年第 3 季度', researched: false, ignored: false, ...o,
});

describe('earnings prompts', () => {
  it('post-earnings prompt names the ticker, period, date and the no-write rule', () => {
    const p = earningsPrompt(item());
    expect(p).toContain('TSM.US 的 2026 财年第 3 季度财报已于 2026-10-14 盘后发布');
    expect(p).toContain('frameworks/TSM.US.md');
    expect(p).toContain('确认 / 削弱 / 证伪');
    expect(p).toContain('落盘等我说「可以落盘」');
  });
  it('pre-earnings prompt for an upcoming release', () => {
    const p = earningsPrompt(item({ status: 'upcoming', slot: 'before', date: '2026-10-16' }));
    expect(p).toContain('TSM.US 将于 2026-10-16 盘前发布 2026 财年第 3 季度财报');
    expect(p).toContain('一致预期');
    expect(p).not.toContain('可以落盘');
  });
  it('an unknown slot says only the date', () => {
    expect(earningsPrompt(item({ slot: 'unknown' }))).toContain('已于 2026-10-14 发布');
  });
  it('keeps an unsent draft and appends the prompt', () => {
    expect(withPrompt('', 'P')).toBe('P');
    expect(withPrompt('   \n', 'P')).toBe('P');
    expect(withPrompt('先看毛利率\n', 'P')).toBe('先看毛利率\n\nP');
  });
});
```

In `app/web/src/route.test.ts`, add `earningsHash` to the import and this case:

```ts
  it('parses the earnings page', () => {
    expect(parseRoute('#/earnings')).toEqual({ page: 'earnings' });
    expect(parseRoute(earningsHash())).toEqual({ page: 'earnings' });
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/neko/plat1ko/lynch/app/web && npx vitest run src/earnings src/route.test.ts`
Expected: FAIL — cannot resolve `./model`, `./prompts`; `earningsHash` is not exported.

- [ ] **Step 3: Write the data layer**

`app/web/src/earnings/types.ts`:

```ts
export type EarningsSlot = 'before' | 'during' | 'after' | 'unknown';

/** One pool ticker's earnings row, judged by the server from the calendar and the market clock. */
export interface EarningsItem {
  ticker: string;
  status: 'released' | 'upcoming';
  date: string; // market-local release date, YYYY-MM-DD
  day_offset: number; // release date minus the market's local today
  slot: EarningsSlot;
  release_at: string; // ISO moment the release counts as out
  period: string; // the calendar's wording, e.g. "2026 财年第 3 季度"
  researched: boolean; // a 讨论 chat on the ticker was started after the release
  ignored: boolean;
}

export interface EarningsList {
  fetched_day: string | null; // Beijing day of the last full calendar fetch
  error: string | null; // why the latest fetch failed; items then come from fetched_day
  items: EarningsItem[]; // released newest first, then upcoming soonest first
}
```

`app/web/src/earnings/api.ts`:

```ts
import { get, post } from '../api';
import type { EarningsList } from './types';

export async function fetchEarnings(): Promise<EarningsList> {
  return (await get('/api/earnings')).json();
}

export async function ignoreEarnings(ticker: string, date: string): Promise<void> {
  await post('/api/earnings/ignore', { ticker, date });
}

export async function unignoreEarnings(ticker: string, date: string): Promise<void> {
  await post('/api/earnings/unignore', { ticker, date });
}
```

`app/web/src/earnings/model.ts`:

```ts
import type { EarningsItem, EarningsList, EarningsSlot } from './types';

export const SLOT_CN: Record<EarningsSlot, string> = { before: '盘前', during: '盘中', after: '盘后', unknown: '' };
const NEAR_DAY: Record<number, string> = { [-1]: '昨天', 0: '今天', 1: '明天' };

/** Out, and neither researched nor dismissed: what the ⋯ badge and the menu count. */
export function isPending(it: EarningsItem): boolean {
  return it.status === 'released' && !it.researched && !it.ignored;
}

export function pendingCount(list: EarningsList | null): number {
  return list ? list.items.filter(isPending).length : 0;
}

export function menuLabel(n: number): string {
  return n > 0 ? `财报 · ${n} 待看` : '财报';
}

export function whenText(it: EarningsItem): string {
  return `${NEAR_DAY[it.day_offset] ?? it.date.slice(5)}${SLOT_CN[it.slot]}`;
}

export function doneTag(it: EarningsItem): string | null {
  if (it.researched) return '已调研';
  if (it.ignored) return '已忽略';
  return null;
}
```

`app/web/src/earnings/prompts.ts`:

```ts
import { SLOT_CN } from './model';
import type { EarningsItem } from './types';

/** The built-in research prompt, sent as the chat's first message so it stays visible in the conversation. */
export function earningsPrompt(it: EarningsItem): string {
  // "2026-10-14 盘后" with a known slot, "2026-10-14 " without: the template follows with 发布.
  const when = SLOT_CN[it.slot] ? `${it.date} ${SLOT_CN[it.slot]}` : `${it.date} `;
  const fw = `frameworks/${it.ticker}.md`;
  if (it.status === 'released') {
    return [
      `${it.ticker} 的 ${it.period}财报已于 ${when}发布。请做财报后核对：`,
      `1. 读 ${fw} 的现行结论和 Assumptions。`,
      '2. 拉本期财报、管理层指引和电话会要点（注明来源）。',
      '3. 逐条对照仍为 active 的假设：确认 / 削弱 / 证伪，给出数字证据和来源。',
      '4. 指出 §3 可演算块里哪些输入需要改，估计 L/M/H 大致往哪边移、为什么。',
      '5. 给结论：框架是否需要修订、改哪几处。',
      '',
      '只在对话里讨论，不写文件；落盘等我说「可以落盘」，再走 sediment-framework。',
    ].join('\n');
  }
  return [
    `${it.ticker} 将于 ${when}发布 ${it.period}财报。请做财报前准备：`,
    `1. 读 ${fw} 的现行结论和 Assumptions。`,
    '2. 列出这次财报会检验的假设；每条写清看哪个数字、到什么程度算确认、到什么程度算削弱。',
    '3. 对照市场一致预期和框架的中性情景（M），指出分歧最大的地方。',
    '4. 留一份财报出来后回来核对的清单。',
    '',
    '只在对话里讨论，不写文件。',
  ].join('\n');
}

/** An unsent draft is the user's own text: keep it and put the prompt after it. */
export function withPrompt(draft: string, prompt: string): string {
  return draft.trim() ? `${draft.trimEnd()}\n\n${prompt}` : prompt;
}
```

`route.ts`: add `| { page: 'earnings' }` to `Route`, in `parseRoute` add `if (hash === '#/earnings') return { page: 'earnings' };` next to the `#/candidates` check, and:

```ts
export function earningsHash(): string {
  return '#/earnings';
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/neko/plat1ko/lynch/app/web && npx vitest run`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /home/neko/plat1ko/lynch
git add app/web/src/earnings/ app/web/src/route.ts app/web/src/route.test.ts
git commit -m "feat(app): earnings data layer, labels and built-in research prompts"
```

---

### Task 6: Earnings page, ⋯ badge, menu row, polling

**Files:**
- Create: `app/web/src/earnings/EarningsPage.tsx`
- Modify: `app/web/src/home/HomePage.tsx`, `app/web/src/App.tsx`, `app/web/src/styles.css`

**Interfaces:**
- Consumes: everything from Task 5; `loadDraft`, `saveDraft` from `src/chat/drafts.ts`; `newChatHash`, `docHash`, `earningsHash` from `src/route.ts`; `tickerLabel`-based `labels` map already built in `App.tsx`.
- Produces: `EarningsPage` component; `HomePage` props `earningsPending: number`, `onOpenEarnings: () => void`.

- [ ] **Step 1: Write `EarningsPage.tsx`**

```tsx
import { loadDraft, saveDraft } from '../chat/drafts';
import { doneTag, isPending, whenText } from './model';
import { earningsPrompt, withPrompt } from './prompts';
import type { EarningsItem, EarningsList } from './types';

/** Pool tickers that just released or are about to release earnings, each a tap away from a research chat. */
export function EarningsPage({ list, error, label, onBack, onResearch, onIgnore, onUnignore, onOpenDoc }: {
  list: EarningsList | null;
  error: string | null; // the page's own request failed
  label: (ticker: string) => string;
  onBack: () => void;
  onResearch: (ticker: string) => void; // opens the ticker's new 讨论 page, whose draft was just written
  onIgnore: (it: EarningsItem) => void;
  onUnignore: (it: EarningsItem) => void;
  onOpenDoc: (ticker: string) => void;
}) {
  const released = list?.items.filter((it) => it.status === 'released') ?? [];
  const upcoming = list?.items.filter((it) => it.status === 'upcoming') ?? [];
  const research = (it: EarningsItem) => {
    const key = `new/${it.ticker}`; // ChatPage's draft key for a new 讨论 chat on this ticker
    saveDraft(key, withPrompt(loadDraft(key), earningsPrompt(it)));
    onResearch(it.ticker);
  };

  const row = (it: EarningsItem) => {
    const tag = doneTag(it);
    return (
      <div key={it.ticker} className={`cand-row ${tag ? 'earn-done' : ''}`}>
        <button className="chat-row-main earn-main" onClick={() => onOpenDoc(it.ticker)}>
          <span className="chat-row-title">{label(it.ticker)}</span>
          <span className="earn-sub">{it.period} · {whenText(it)}{tag ? ` · ${tag}` : ''}</span>
        </button>
        <button className="text-btn" onClick={() => research(it)}>调研</button>
        {isPending(it) && <button className="x-btn" aria-label={`忽略 ${label(it.ticker)}`} onClick={() => onIgnore(it)}>×</button>}
        {it.status === 'released' && it.ignored && !it.researched && (
          <button className="text-btn" onClick={() => onUnignore(it)}>恢复</button>
        )}
      </div>
    );
  };

  return (
    <div className="chats">
      <header className="bar bar-top">
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <div className="chat-title-main">财报</div>
        </div>
      </header>
      <main className="chats-body">
        {error && <div className="banner-error">{error}</div>}
        {list?.error && (
          <div className="banner-error">
            财报日历刷新失败：{list.error}{list.fetched_day ? `（显示的是 ${list.fetched_day} 的数据）` : ''}
            ；登录过期时在终端运行 longbridge auth login
          </div>
        )}
        {released.length > 0 && (
          <section>
            <h3 className="chats-caption">已出</h3>
            {released.map(row)}
          </section>
        )}
        {upcoming.length > 0 && (
          <section>
            <h3 className="chats-caption">即将发布</h3>
            {upcoming.map(row)}
          </section>
        )}
        {list && list.items.length === 0 && <p className="empty">近 7 天没有发布、3 天内也没有要发布的财报</p>}
        {!list && !error && <p className="empty">加载中…</p>}
      </main>
    </div>
  );
}
```

- [ ] **Step 2: Styles**

Append to `app/web/src/styles.css` (in the `/* ---- chats */` area, after the `.cand-*` rules at the end is fine):

```css
/* ---- earnings */
/* A second, quieter line under the ticker: period and when. */
.earn-main { flex-direction: column; align-items: flex-start; gap: 2px; }
.earn-main .chat-row-title { width: 100%; }
.earn-sub { color: var(--muted); font-size: 12px; }
.earn-done .chat-row-main { opacity: 0.55; }
/* Left of the ⋯ button, so the running-chats count keeps the right corner. */
.badge-alert { right: auto; left: -6px; min-width: 18px; padding: 0; background: #e5484d; font-weight: 700; }
```

- [ ] **Step 3: ⋯ badge and menu row in `HomePage.tsx`**

Add props to the destructuring and the type:

```tsx
export function HomePage({ catalog, filter, onFilter, onOpen, onOpenAxis, onRefreshQuotes, onRefreshFrameworks,
  refreshing, chatCount, onOpenChats, earningsPending, onOpenEarnings }: {
  …
  chatCount: number;
  onOpenChats: () => void;
  earningsPending: number; // released earnings neither researched nor dismissed
  onOpenEarnings: () => void;
}) {
```

Import: `import { menuLabel } from '../earnings/model';`

Replace the ⋯ button and the first menu item:

```tsx
          {/* Badges stay on the button so running chats and pending earnings show without opening the menu. */}
          <button className="icon-btn has-badge" onClick={() => setMenu(!menu)} aria-label="更多">
            ⋯{chatCount > 0 && <span className="badge">{chatCount}</span>}
            {earningsPending > 0 && <span className="badge badge-alert">!</span>}
          </button>
          {menu && (
            <Menu onClose={() => setMenu(false)} items={[
              { label: menuLabel(earningsPending), onSelect: onOpenEarnings },
              { label: chatCount > 0 ? `对话 · ${chatCount} 个运行中` : '对话', onSelect: onOpenChats },
```

(the remaining items are unchanged).

- [ ] **Step 4: Wire `App.tsx`**

Imports:

```tsx
import { EarningsPage } from './earnings/EarningsPage';
import { fetchEarnings, ignoreEarnings, unignoreEarnings } from './earnings/api';
import { pendingCount } from './earnings/model';
import type { EarningsList } from './earnings/types';
```

and add `earningsHash` to the existing `./route` import.

After the candidates state block, add:

```tsx
  const [earnings, setEarnings] = useState<EarningsList | null>(null);
  const [earningsError, setEarningsError] = useState<string | null>(null);
  const loadEarnings = useCallback(() => {
    fetchEarnings().then((l) => { setEarnings(l); setEarningsError(null); },
      (e: unknown) => setEarningsError(String(e instanceof Error ? e.message : e)));
  }, []);
  // Released / upcoming follow the clock (a release flips at the open or close), so poll; the server answers
  // from its day's calendar cache without calling longbridge.
  useEffect(() => {
    if (route.page !== 'home' && route.page !== 'earnings') return;
    loadEarnings();
    const timer = window.setInterval(loadEarnings, EARNINGS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [route.page, loadEarnings]);
  const dismiss = (p: Promise<unknown>) =>
    p.then(loadEarnings, (e: unknown) => setEarningsError(String(e instanceof Error ? e.message : e)));
```

with a module-level constant above `export function App()`:

```tsx
const EARNINGS_POLL_MS = 5 * 60_000;
```

Before the `if (route.page === 'doc')` block, add:

```tsx
  if (route.page === 'earnings') {
    return (
      <EarningsPage list={earnings} error={earningsError} label={(t) => labels.get(t) ?? t} onBack={goBack}
        onResearch={(t) => { window.location.hash = newChatHash(t); }}
        onIgnore={(it) => dismiss(ignoreEarnings(it.ticker, it.date))}
        onUnignore={(it) => dismiss(unignoreEarnings(it.ticker, it.date))}
        onOpenDoc={(t) => { window.location.hash = docHash(t); }} />
    );
  }
```

Note: inside the `doc` branch the code shadows `labels` with a local `const labels = zoneLabels(catalog)`; the earnings branch sits before it and uses the outer ticker-label map.

Pass the new props to `HomePage`:

```tsx
        chatCount={chatList?.running_count ?? 0} onOpenChats={() => openFromHome(chatsHash())}
        earningsPending={pendingCount(earnings)} onOpenEarnings={() => openFromHome(earningsHash())} />
```

- [ ] **Step 5: Type-check, test, build**

Run: `cd /home/neko/plat1ko/lynch/app/web && npm run build && npx vitest run`
Expected: `tsc --noEmit` clean, vite build writes `dist/`, all tests pass.

- [ ] **Step 6: Run the app and check by hand**

```bash
cd /home/neko/plat1ko/lynch
just app-stop; just app-web && just app-start && just app-status
```

Then, on the phone (or a paired browser on the tailnet):
1. Home loads; ⋯ shows「!」only if some release is pending; menu's first row reads「财报」or「财报 · N 待看」.
2. 财报 page shows 已出 / 即将发布 groups (Q3 season: e.g. TSM.US 10-14 within 3 days of today should appear under 即将发布 from 10-11).
3. 调研 opens「讨论 TICKER」with the prompt in the input box; an unsent draft typed earlier stays above it.
4. × marks a row 已忽略 and drops the count; 恢复 undoes it.
5. `ls output/earnings/` shows `calendar.json` (and `ignored.json` after an ignore); a second page load within the day adds no `finance-calendar` call (check `_local/tmp/lynch-app.log` has no new "finance-calendar batch failed" and the file's mtime does not change).

- [ ] **Step 7: Commit**

```bash
cd /home/neko/plat1ko/lynch
git add app/web/src/earnings/EarningsPage.tsx app/web/src/home/HomePage.tsx app/web/src/App.tsx app/web/src/styles.css
git commit -m "feat(app): earnings page behind the ⋯ menu; ⋯ shows ! while a release waits"
```

---

## Final verification

```bash
cd /home/neko/plat1ko/lynch
python3 -m unittest discover -s eval/lib -t . -p 'test_*.py'
(cd app/server && uv run pytest -q)
(cd app/web && npx vitest run)
(cd frameworks && just check)   # unaffected, run because eval/lib changed
crontab -l | grep lynch-        # only the two daily-bands lines
git status --short              # nothing under _local/ staged
```
