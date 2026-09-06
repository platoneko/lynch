"""Read/write output/bands/<TICKER>.json artifacts."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from eval.lib.zones import cut_points

REPO_ROOT = Path(__file__).resolve().parents[2]
BANDS_DIR = REPO_ROOT / "output" / "bands"


def bands_path(ticker: str, bands_dir: Path | None = None) -> Path:
    return (bands_dir or BANDS_DIR) / f"{ticker}.json"


def read_previous(ticker: str, bands_dir: Path | None = None) -> dict[str, Any] | None:
    path = bands_path(ticker, bands_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build_record(
    *,
    ticker: str,
    as_of: str,
    price: float,
    currency: str,
    L: float,
    M: float,
    H: float,
    zone: str,
    previous_zone: str | None,
    formula_id: str = "framework_anchors_v1",
    notes: list[str] | None = None,
    inputs_used: dict[str, Any] | None = None,
) -> dict[str, Any]:
    A, B = cut_points(L, M, H)
    return {
        "ticker": ticker,
        "as_of": as_of,
        "price": price,
        "currency": currency,
        "anchors": {"L": L, "M": M, "H": H, "A": A, "B": B},
        "bands": {
            "deep_discount": {"max_exclusive": L},
            "conservative": {"min_inclusive": L, "max_exclusive": A},
            "neutral": {"min_inclusive": A, "max_exclusive": B},
            "optimistic": {"min_inclusive": B, "max_inclusive": H},
            "stretched": {"min_exclusive": H},
        },
        "zone": zone,
        "previous_zone": previous_zone,
        "inputs_used": inputs_used or {"price_source": "longbridge", "anchors_source": "frameworks"},
        "formula_id": formula_id,
        "notes": notes or [],
    }


def write_bands(record: dict[str, Any], bands_dir: Path | None = None) -> Path:
    ticker = str(record["ticker"])
    path = bands_path(ticker, bands_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
