"""Ticker rows for the reader: framework header fields plus the latest band artifact."""
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.bands_io import BANDS_DIR, bands_path, read_previous  # noqa: E402
from eval.lib.frameworks import (  # noqa: E402
    FRAMEWORKS_DIR,
    LYNCH_CN,
    list_framework_paths,
    load_framework,
    market_bucket,
)
from eval.lib.zones import ZONE_CN, ZONE_ORDER  # noqa: E402

__all__ = ["BANDS_DIR", "FRAMEWORKS_DIR", "REPO_ROOT", "catalog", "framework_path"]

log = logging.getLogger("lynch_reader")

ROW_KEYS = (
    "ticker", "name", "market", "lynch_primary", "lynch_secondary", "currency",
    "L", "M", "H", "blocked", "candidate_label", "band", "error",
)
# Only what a card shows; the rest of the artifact belongs to the daily digest.
BAND_KEYS = ("price", "as_of", "zone", "option_zone", "candidate_zone")


def catalog(frameworks_dir: Path, bands_dir: Path) -> dict[str, Any]:
    return {
        "zones": [{"id": z, "label": ZONE_CN[z]} for z in ZONE_ORDER],
        "lynch_classes": [{"id": k, "label": v} for k, v in LYNCH_CN.items()],
        "tickers": [_ticker_row(p, bands_dir) for p in list_framework_paths(frameworks_dir)],
    }


def framework_path(ticker: str, frameworks_dir: Path) -> Path | None:
    """Resolve only pool members; the requested name is never joined into a path."""
    for p in list_framework_paths(frameworks_dir):
        if p.stem == ticker:
            return p
    return None


def _ticker_row(path: Path, bands_dir: Path) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(ROW_KEYS)
    row["ticker"] = path.stem
    try:
        meta = load_framework(path)
    except Exception as e:  # noqa: BLE001 — one bad header must not blank the whole list
        log.warning("framework parse failed: %s: %s", path.name, e)
        row["error"] = str(e)
        return row
    row.update(
        name=meta.name,
        market=market_bucket(meta.ticker),
        lynch_primary=meta.lynch_primary,
        lynch_secondary=meta.lynch_secondary,
        currency=meta.currency,
        L=meta.L,
        M=meta.M,
        H=meta.H,
        blocked=meta.blocked,
        candidate_label=meta.candidate_label,
        band=_read_band(meta.ticker, bands_dir),
    )
    return row


def _read_band(ticker: str, bands_dir: Path) -> dict[str, Any] | None:
    # read_previous only absorbs OSError/JSONDecodeError; a non-UTF-8 file
    # (UnicodeDecodeError, a ValueError) must also cost just this row.
    try:
        record = read_previous(ticker, bands_dir)
    except (ValueError, OSError) as e:
        log.warning("band artifact unreadable: %s: %s", ticker, e)
        return None
    if record is None:
        if bands_path(ticker, bands_dir).exists():
            log.warning("band artifact unreadable: %s", ticker)
        return None
    if not isinstance(record, dict):
        log.warning("band artifact is not an object: %s", ticker)
        return None
    price = record.get("price")
    # json.loads accepts NaN/Infinity, which the JSON response cannot carry.
    if (isinstance(price, bool) or not isinstance(price, (int, float))
            or not math.isfinite(price) or "as_of" not in record):
        log.warning("band artifact lacks price/as_of: %s", ticker)
        return None
    return {k: record.get(k) for k in BAND_KEYS}
