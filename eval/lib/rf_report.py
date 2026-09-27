"""The r_f readings block every rate-mounted eval prints (valuation-rules「利率轴与 r_f 敏感性」).

Only the ticker knows how to re-price its model at another r, so it hands in
`band_at(dr) -> (L, M, H)`: the anchors with EVERY priced r shifted by dr (Δr_f
moves each r 1:1; g, ROE and the numerator stay put).  This module owns the
rest — which columns exist, how they print, what the daily digest receives —
so no eval carries its own copy.
"""
from __future__ import annotations

from typing import Callable, NamedTuple

from eval.lib.axes import load_rates

Band = tuple[float, float, float]


class RfBlock(NamedTuple):
    seg: dict[str, float]
    consensus: Band
    pressure: Band


def rf_block(
    ccy: str,
    priced_r: dict[str, float],
    band_at: Callable[[float], Band],
) -> RfBlock:
    """Print the header, the ±step / consensus rows and the reverse-pressure row.

    `priced_r` names every r the anchors use ({"r_perp": 0.10, ...}); each is
    decomposed as 中枢 + 篮子溢价 so a reader sees what moves.  The dr = 0 row
    MUST reproduce the base anchors — callers check that, since only they hold them.
    No zone: framework docs carry no current price.
    """
    seg = load_rates(ccy)
    mid, step, cap, delta = seg["rf_mid"], seg["rf_step"], seg["rf_cap"], seg["delta_cons"]
    parts = "  ".join(f"{k} = {mid:.2%} + {r - mid:.2%} = {r:.2%}" for k, r in priced_r.items())
    print(
        f"r_f readings ({ccy}, _AXIS.rates, disclosure only): {parts}"
        f"   | 10Y {seg['rf_now']:.2%}; 共识 r_f {seg['rf_cons']:.2%} (Δ {delta * 1e4:+.0f}bp)"
    )
    base = band_at(0.0)
    for label, dr in ((f"r_f {mid - step:.2%}", -step), (f"r_f {mid:.2%}", 0.0),
                      (f"r_f {mid + step:.2%}", step), (f"共识 {seg['rf_cons']:.2%}", delta)):
        L, M, H = band_at(dr)
        print(
            f"  {label:<12} Δ {dr * 1e4:+4.0f}bp   L/M/H {L:.2f}/{M:.2f}/{H:.2f}"
            f" ({M / base[1] - 1:+.1%} on M)"
            + ("   <- 中枢 = 主 zone" if dr == 0 else "")
        )
    consensus = band_at(delta)
    # Reverse pressure is directionless (|Δ| <= cap): L takes +cap, M 0, H −cap.
    pressure = (band_at(cap)[0], base[1], band_at(-cap)[2])
    print(
        f"  反向压力 ±{cap * 1e4:.0f}bp   L/M/H {pressure[0]:.2f}/{pressure[1]:.2f}/{pressure[2]:.2f}"
    )
    return RfBlock(seg, consensus, pressure)
