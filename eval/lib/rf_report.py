"""The r_f readings block every rate-mounted eval prints (valuation-rules「利率轴与 r_f 敏感性」).

Only the ticker knows how to re-price its model at another r, so it hands in
`band_at(dr) -> (L, M, H)`: the anchors with EVERY priced r shifted by dr (Δr_f
moves each r 1:1; g, ROE and the numerator stay put).  This module owns the
rest — which columns exist, how they print, what the daily digest receives —
so no eval carries its own copy.
"""
from __future__ import annotations

from math import isclose, isfinite
import re
from typing import Callable, NamedTuple

Band = tuple[float, float, float]


def load_rates(ccy: str) -> dict[str, float]:
    """Legacy-only compatibility until the old consumers migrate together."""
    from eval.lib.axes import load_rates as legacy_load
    return legacy_load(ccy)


class RfBlock(NamedTuple):
    seg: dict[str, float]
    consensus: Band
    pressure: Band


def axis_loader(axis: dict) -> Callable[[str], dict[str, float]]:
    """Bind the axis snapshot supplied by the runner; never infer FX."""
    def load(currency: str) -> dict[str, float]:
        try:
            segment = axis["segments"][currency]
            if segment["status"] not in ("built", "linked"):
                raise ValueError(f"rates axis {currency} segment not built")
            return segment["values"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"missing rates axis segment: {currency}") from exc
    return load


def _checked_band(band: Band) -> Band:
    try:
        values = tuple(band)
        if len(values) != 3 or any(isinstance(x, bool) or not isfinite(x) for x in values):
            raise ValueError("band must contain three finite prices")
        return values
    except (TypeError, OverflowError) as exc:
        raise ValueError("band must contain three finite prices") from exc


def _calculate(
    rate_currency: str,
    priced_r: dict[str, float],
    band_at: Callable[[float], Band],
    rates_loader: Callable[[str], dict[str, float]],
    base_anchors: Band | None,
    *, require_anchors: bool,
) -> dict:
    # Compute every scenario before producing any report.  The model must
    # enforce its own financial domain (e.g. r > g) and be free of I/O.
    seg = rates_loader(rate_currency)
    try:
        mid, step, cap, delta, now, consensus_rf = (seg[k] for k in
            ("rf_mid", "rf_step", "rf_cap", "delta_cons", "rf_now", "rf_cons"))
        if (any(isinstance(x, bool) or not isfinite(x) for x in (mid, step, cap, delta, now, consensus_rf))
                or step not in (0.0025, 0.005) or cap <= 0
                or not isclose(consensus_rf - mid, delta, abs_tol=1e-9)):
            raise ValueError("invalid rates segment")
        if not priced_r or any(not isinstance(k, str) or not k.strip() for k in priced_r):
            raise ValueError("priced rates must name at least one discount rate")
        if any(isinstance(r, bool) or not isfinite(r) for r in priced_r.values()):
            raise ValueError("invalid priced rate")
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("invalid rates segment or priced rate") from exc
    base = _checked_band(band_at(0.0))
    if require_anchors:
        anchors = _checked_band(base_anchors) if base_anchors is not None else None
        if anchors is None or any(not isclose(a, b, rel_tol=1e-9, abs_tol=0.0)
                                  for a, b in zip(anchors, base)):
            raise ValueError("rates sensitivity baseline does not match base anchors")
    bands = {0.0: base}
    for dr in (-step, step, delta, cap, -cap):
        if dr not in bands:
            bands[dr] = _checked_band(band_at(dr))

    return {
        "seg": dict(seg), "priced_r": dict(priced_r), "base": base,
        "step_down": bands[-step], "step_up": bands[step],
        "consensus": bands[delta],
        "pressure": (bands[cap][0], base[1], bands[-cap][2]),
    }


def calculate_rf_readings(
    *, rate_currency: str, price_currency: str, price_unit: str,
    priced_r: dict[str, float], band_at: Callable[[float], Band],
    rates_loader: Callable[[str], dict[str, float]], base_anchors: Band,
) -> dict:
    """Calculate full-precision readings without printing or converting currency.

    ``rate_currency`` selects the risk-rate segment. ``price_currency`` is the
    product's listing currency; all anchors and callback bands are prices per
    share or fund unit as identified by ``price_unit``. The caller is
    responsible for supplying matching units and for each model's domain.
    """
    if (not isinstance(rate_currency, str) or not re.fullmatch(r"[A-Z]{3}", rate_currency)
            or not isinstance(price_currency, str) or not re.fullmatch(r"[A-Z]{3}", price_currency)
            or price_unit not in ("per_share", "per_fund_unit")):
        raise ValueError("rate currency, product price currency and price unit are required")
    calc = _calculate(rate_currency, priced_r, band_at, rates_loader, base_anchors, require_anchors=True)
    # String keys preserve scenario meaning when the result is encoded as JSON.
    return {
        "rate_currency": rate_currency, "price_currency": price_currency, "price_unit": price_unit,
        "segment": calc["seg"], "priced_r": calc["priced_r"], "base": calc["base"],
        "step_down": calc["step_down"], "step_up": calc["step_up"],
        "consensus": calc["consensus"], "pressure": calc["pressure"],
    }


def result_rate_sensitivity(readings: dict) -> dict:
    """Map numeric rate readings to the persisted price-band contract."""
    return {
        **readings,
        "inputs": dict(readings["segment"]),
        "consensus": dict(zip("LMH", readings["consensus"])),
        "pressure": dict(zip("LMH", readings["pressure"])),
    }


def _legacy_text(ccy: str, calc: dict) -> str:
    """Original two-decimal text, retained only for old consumers and parity."""
    seg, priced_r = calc["seg"], calc["priced_r"]
    mid, step, cap, delta = (seg[k] for k in ("rf_mid", "rf_step", "rf_cap", "delta_cons"))
    now, consensus_rf = seg["rf_now"], seg["rf_cons"]
    base_m = calc["base"][1]
    if base_m == 0:
        raise ValueError("base M is zero; percentage change is undefined")
    changes = {dr: band[1] / base_m - 1 for dr, band in
               ((-step, calc["step_down"]), (0.0, calc["base"]),
                (step, calc["step_up"]), (delta, calc["consensus"]))}
    display_values = [mid * 100, (mid - step) * 100, (mid + step) * 100,
                      now * 100, consensus_rf * 100, delta * 1e4, cap * 1e4]
    display_values += [value * 100 for value in changes.values()]
    display_values += [value for r in priced_r.values() for value in (r * 100, (r - mid) * 100)]
    if any(not isfinite(value) for value in display_values):
        raise ValueError("non-finite derived rate or percentage change")

    parts = "  ".join(f"{k} = {mid:.2%} + {r - mid:.2%} = {r:.2%}" for k, r in priced_r.items())
    lines = [
        f"r_f readings ({ccy}, _AXIS.rates, disclosure only): {parts}"
        f"   | 10Y {seg['rf_now']:.2%}; 共识 r_f {seg['rf_cons']:.2%} (Δ {delta * 1e4:+.0f}bp)"
    ]
    for label, dr, band in ((f"r_f {mid - step:.2%}", -step, calc["step_down"]),
                            (f"r_f {mid:.2%}", 0.0, calc["base"]),
                            (f"r_f {mid + step:.2%}", step, calc["step_up"]),
                            (f"共识 {seg['rf_cons']:.2%}", delta, calc["consensus"])):
        L, M, H = band
        lines.append(
            f"  {label:<12} Δ {dr * 1e4:+4.0f}bp   L/M/H {L:.2f}/{M:.2f}/{H:.2f}"
            f" ({changes[dr]:+.1%} on M)"
            + ("   <- 中枢 = 主 zone" if dr == 0 else "")
        )
    pressure = calc["pressure"]
    lines.append(
        f"  反向压力 ±{cap * 1e4:.0f}bp   L/M/H {pressure[0]:.2f}/{pressure[1]:.2f}/{pressure[2]:.2f}"
    )
    return "\n".join(lines)


def rf_block(
    ccy: str, priced_r: dict[str, float], band_at: Callable[[float], Band], *,
    rates_loader: Callable[[str], dict[str, float]] | None = None,
    base_anchors: Band | None = None,
) -> RfBlock:
    """Legacy text adapter; new consumers should use ``calculate_rf_readings``."""
    if rates_loader is None:
        rates_loader = load_rates
        require_anchors = False
    else:
        require_anchors = True
    calc = _calculate(ccy, priced_r, band_at, rates_loader, base_anchors,
                      require_anchors=require_anchors)
    text = _legacy_text(ccy, calc)
    result = RfBlock(calc["seg"], calc["consensus"], calc["pressure"])
    print(text)
    return result
