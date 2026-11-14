"""Read/write output/rates/<TICKER>.json artifacts."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
RATES_DIR = REPO_ROOT / "output" / "rates"



# --- r_f readings (valuation-rules「利率轴与 r_f 敏感性」) -----------------------
# Only the eval can re-price a ticker at another r (the map is the whole model,
# not a scalar), so it writes the two reading bands here. The file records what
# it was priced on: when the axis Δ / cap or framework anchors move, it is stale.
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
