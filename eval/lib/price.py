"""Fetch quotes and daily bars via longbridge CLI."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# US extended-hours blocks of a quote row, by the session id this module reports.
EXTENDED_SESSIONS = {"pre": "pre_market", "post": "post_market", "overnight": "overnight"}


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


def _parse_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


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


def fetch_quotes(
    symbols: list[str], *, timeout_s: float = 60.0, allow_missing: bool = False
) -> dict[str, Quote]:
    """Batch-quote symbols. Keys are the requested symbol strings.

    allow_missing lets a caller that reports per-symbol failures (the daily
    digest) keep going when one ticker is suspended or unknown, instead of
    losing the whole run to it.
    """
    if not symbols:
        return {}
    data = _run(["longbridge", "quote", *symbols, "--format", "json"], timeout_s)
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected quote payload type: {type(data)}")

    out: dict[str, Quote] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "")
        last = _parse_float(row.get("last"))
        if not sym or last is None:
            continue
        out[sym] = Quote(
            symbol=sym,
            last=last,
            prev_close=_parse_float(row.get("prev_close")),
            status=str(row.get("status")) if row.get("status") is not None else None,
            extended=_extended(row),
            volume=_parse_float(row.get("volume")),
        )
    missing = [s for s in symbols if s not in out]
    if missing and not allow_missing:
        raise RuntimeError(f"missing quotes for: {', '.join(missing)}")
    return out


def _extended(row: dict) -> tuple[SessionQuote, ...]:
    """The extended-hours blocks that carry both a price and a trade time; absent for non-US rows."""
    out = []
    for session, key in EXTENDED_SESSIONS.items():
        block = row.get(key)
        if not isinstance(block, dict):
            continue
        last = _parse_float(block.get("last"))
        ts = block.get("timestamp")
        if last is None or not ts:
            continue
        out.append(SessionQuote(session, last, datetime.fromisoformat(str(ts).replace("Z", "+00:00")),
                                _parse_float(block.get("prev_close"))))
    return tuple(out)


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
