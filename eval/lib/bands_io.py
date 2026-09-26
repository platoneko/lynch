"""Read/write output/bands/<TICKER>.json and output/rates/<TICKER>.json artifacts."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from eval.lib.zones import cut_points

REPO_ROOT = Path(__file__).resolve().parents[2]
BANDS_DIR = REPO_ROOT / "output" / "bands"
RATES_DIR = REPO_ROOT / "output" / "rates"


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
    zone: str | None,
    previous_zone: str | None,
    formula_id: str = "framework_anchors_v1",
    notes: list[str] | None = None,
    inputs_used: dict[str, Any] | None = None,
    option_zone: str | None = None,
    previous_option_zone: str | None = None,
    candidate_zone: str | None = None,
    previous_candidate_zone: str | None = None,
    blocked: str | None = None,
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
        "option_zone": option_zone,
        "previous_option_zone": previous_option_zone,
        "candidate_zone": candidate_zone,
        "previous_candidate_zone": previous_candidate_zone,
        "blocked": blocked,
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


# --- r_f readings (valuation-rules「利率轴与 r_f 敏感性」) -----------------------
# Only the eval can re-price a ticker at another r (the map is the whole model,
# not a scalar), so it writes the two reading bands here and the daily digest
# reads them.  The file records what it was priced on: when the axis Δ / cap or
# the framework anchors move, the reading is stale and must not print numbers.
_RF_KEYS = ("delta_cons", "rf_cap")
# Frameworks state anchors at display precision (445, 505.4) while the eval
# writes the unrounded value, so compare relatively; a real re-anchoring moves
# far more than this.
_RF_REL_TOL = 5e-3


def write_rf_readings(
    ticker: str,
    seg: dict[str, float],
    base: tuple[float, float, float],
    consensus: tuple[float, float, float],
    pressure: tuple[float, float, float],
    rates_dir: Path | None = None,
) -> Path:
    path = (rates_dir or RATES_DIR) / f"{ticker}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ticker": ticker,
        "priced_on": {k: seg[k] for k in _RF_KEYS},
        "base": list(base),
        "consensus": list(consensus),
        "pressure": list(pressure),
    }
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_rf_readings(
    ticker: str,
    seg: dict[str, float],
    base: tuple[float, float, float],
    rates_dir: Path | None = None,
) -> dict[str, Any] | None:
    """None when the eval never wrote one; `stale` when axis or anchors moved since."""
    path = (rates_dir or RATES_DIR) / f"{ticker}.json"
    if not path.is_file():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"stale": True}
    stale = any(abs(rec["priced_on"][k] - seg[k]) > 1e-9 for k in _RF_KEYS) or any(
        abs(a - b) > _RF_REL_TOL * abs(b) for a, b in zip(rec["base"], base)
    )
    return {
        "stale": stale,
        "delta_cons": rec["priced_on"]["delta_cons"],
        "rf_cap": rec["priced_on"]["rf_cap"],
        "consensus": tuple(rec["consensus"]),
        "pressure": tuple(rec["pressure"]),
    }
