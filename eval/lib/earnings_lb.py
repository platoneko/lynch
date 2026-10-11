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
