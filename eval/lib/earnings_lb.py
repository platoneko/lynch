"""Longbridge CLI fetchers for earnings calendar + latest report period."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import date, timedelta
from typing import Any

from eval.lib.earnings_available import calendar_lookup_key, normalize_symbol

log = logging.getLogger(__name__)

_CAL_BATCH = 10
_HTTP_TIMEOUT_S = 60.0


def _require_cli() -> None:
    if not shutil.which("longbridge"):
        raise RuntimeError("longbridge CLI not found on PATH")


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
    dec = json.JSONDecoder()
    data, _ = dec.raw_decode(text[start:])
    return data


def _run_json(cmd: list[str], *, timeout_s: float = _HTTP_TIMEOUT_S) -> Any:
    _require_cli()
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"longbridge failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )
    return _parse_json_stdout(proc.stdout)


def fetch_latest_period(symbol: str, *, timeout_s: float = _HTTP_TIMEOUT_S) -> str | None:
    """Return report period id (e.g. '2026.H1') or None."""
    data = _run_json(
        ["longbridge", "financial-report", symbol, "--latest", "--format", "json"],
        timeout_s=timeout_s,
    )
    if not isinstance(data, dict):
        return None
    report = data.get("report")
    if report is None:
        return None
    s = str(report).strip()
    return s or None


def fetch_latest_periods(
    tickers: list[str],
    *,
    timeout_s: float = _HTTP_TIMEOUT_S,
) -> dict[str, str]:
    """Map framework ticker → latest period. Failures are omitted (logged)."""
    out: dict[str, str] = {}
    for t in tickers:
        try:
            period = fetch_latest_period(t, timeout_s=timeout_s)
        except Exception as e:  # noqa: BLE001
            log.warning("latest period failed %s: %s", t, e)
            continue
        if period:
            out[t] = period
    return out


def _iter_calendar_infos(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    infos: list[dict[str, Any]] = []
    for day in payload.get("list") or []:
        if not isinstance(day, dict):
            continue
        for info in day.get("infos") or []:
            if isinstance(info, dict):
                infos.append(info)
    return infos


def _info_local_date(info: dict[str, Any]) -> date | None:
    ext = info.get("ext") if isinstance(info.get("ext"), dict) else {}
    raw = ext.get("local_date") or info.get("widget_short_display_date")
    if not raw:
        return None
    s = str(raw).strip()
    # local_date is YYYY-MM-DD; widget form is MM/DD — skip non-ISO.
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def fetch_earn_dates_in_window(
    tickers: list[str],
    *,
    today: date,
    max_d: int,
    timeout_s: float = _HTTP_TIMEOUT_S,
) -> dict[str, str]:
    """For each ticker, pick earn local_date E with 0 <= today-E <= max_d (smallest d wins).

    Keys are framework tickers. Calendar may return zero-stripped HK codes.
    """
    if not tickers:
        return {}

    start = today - timedelta(days=max_d)
    end = today  # only past+today; upcoming (d<0) is outside notify window
    by_norm: dict[str, list[date]] = {}

    for i in range(0, len(tickers), _CAL_BATCH):
        batch = tickers[i : i + _CAL_BATCH]
        cmd = [
            "longbridge",
            "finance-calendar",
            "report",
            "--format",
            "json",
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
            "--count",
            "100",
        ]
        for t in batch:
            cmd.extend(["--symbol", t])
        try:
            payload = _run_json(cmd, timeout_s=timeout_s)
        except Exception as e:  # noqa: BLE001
            log.warning("finance-calendar batch failed %s: %s", batch, e)
            continue
        for info in _iter_calendar_infos(payload):
            sym = str(info.get("symbol") or "").strip()
            if not sym:
                continue
            ed = _info_local_date(info)
            if ed is None:
                continue
            by_norm.setdefault(normalize_symbol(sym), []).append(ed)

    out: dict[str, str] = {}
    for t in tickers:
        dates = by_norm.get(calendar_lookup_key(t)) or []
        in_window = [e for e in dates if 0 <= (today - e).days <= max_d]
        if not in_window:
            continue
        # Prefer most recent earnings day still in window (smallest d).
        best = max(in_window)
        out[t] = best.isoformat()
    return out
