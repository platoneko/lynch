"""Ticker rows for the reader: framework header fields plus the latest band artifact."""
from __future__ import annotations

import logging
import math
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.bands_io import BANDS_DIR, bands_path, read_previous  # noqa: E402
from eval.lib.frameworks import (  # noqa: E402
    _CANDIDATE,
    FRAMEWORKS_DIR,
    FrameworkMeta,
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
    "L", "M", "H", "blocked", "candidate", "band", "error",
)
# Only what a card shows; the rest of the artifact belongs to the daily digest.
BAND_KEYS = ("price", "as_of", "zone", "option_zone", "candidate_zone")

# The candidate line names its gate after the primed anchors: `… · 门槛 A1c（note）`.
_GATE = re.compile(r"门槛\s*(A[0-9A-Za-z]+′?)")
_ASSUMPTIONS = re.compile(r"^## 2\. Assumptions[^\n]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
_CELL_SEP = re.compile(r"(?<!\\)\|")  # `\|` inside a cell is text, not a separator


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
        candidate=_candidate(meta, path.read_text(encoding="utf-8")),
        band=_read_band(meta.ticker, bands_dir),
    )
    return row


def _candidate(meta: FrameworkMeta, md: str) -> dict[str, Any] | None:
    """The candidate band plus why it is only a candidate: its gate assumption's text."""
    if meta.candidate is None:
        return None
    L, M, H = meta.candidate
    gate = _candidate_gate(md)
    return {
        "label": meta.candidate_label, "L": L, "M": M, "H": H,
        "gate": gate, "background": _assumption_text(md, gate) if gate else None,
    }


def _candidate_gate(md: str) -> str | None:
    # Reuse eval's own candidate regex so the gate comes from the very line the anchors did;
    # only the rest of that line after H′ is searched (a label may name an id too).
    m = _CANDIDATE.search(md)
    if m is None:
        return None
    end = md.find("\n", m.end())
    gate = _GATE.search(md, m.end(), len(md) if end < 0 else end)
    return gate.group(1) if gate else None


def _assumption_text(md: str, gate: str) -> str | None:
    """Second cell of the §2 Assumptions row whose first cell is `gate`."""
    section = _ASSUMPTIONS.search(md)
    if section is None:
        return None
    for line in section.group(1).splitlines():
        cells = [c.strip() for c in _CELL_SEP.split(line.strip())]
        # `| id | text | …` splits into ['', 'id', 'text', …].
        if len(cells) >= 3 and cells[0] == "" and cells[1] == gate:
            return cells[2]
    return None


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
