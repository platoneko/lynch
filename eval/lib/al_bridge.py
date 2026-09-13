"""Gross-margin bridge shared by the four aluminium tickers.

1378.HK / 601600.SH / 002532.SZ / 000933.SZ all price off the same ruler,
mounted on `_AXIS.fed-path-aluminium`, which locks the SHFE smelting spread:

    M  = current half-year's measured net profit, annualised.
    H  = M plus (annual smelting volume x the axis's M->H spread step), carried
         to net profit by the *marginal* conversion rate.
    L  = M minus (annual smelting volume x the axis's M->L spread step), the
         same way.  The L world is the axis's supply-glut rung (GS 2027E) with
         the cost basket frozen — i.e. aluminium down, alumina *not* up — so it
         is not the 2025H1 half-year, whose squeeze came from high alumina.

The marginal conversion rate k is still solved from two same-basis measured
points (2025H1 and 2026H1).  That half-year is the *fitting point* for k, not
an anchor: the `*_ref` inputs exist only to pin k, and 2025H1 itself is read
back into each framework as a reference coordinate (a different corner of the
aluminium/alumina grid), not as L.

Only the per-ticker inputs differ, so the arithmetic lives here rather than in
four copies.  What does not live here is each ticker's own guard — Chalco's
minority drain, Tianshan's volume basis, Hongqiao's integration shape —
because those are the parts that are genuinely not shared.
"""

from __future__ import annotations

REQUIRED = [
    "price", "shares",
    "rev_m", "gm_m", "rev_ref", "gm_ref", "np_m", "np_ref",
    "vol_al", "spread_l", "spread_m", "spread_h", "pe",
    "bps", "pb_low",
]

# vol_al is 万吨, spreads are 元/吨, money is 亿元: 万 x 元 = 万元 = 1e-4 亿.
_VOL_PRICE_TO_YI = 1e-4


def bridge(inp: dict[str, float]) -> dict[str, object]:
    """Recompute the three tiers from a framework's §3 Inputs table."""
    gp_ref = inp["rev_ref"] * inp["gm_ref"]
    gp_m = inp["rev_m"] * inp["gm_m"]
    if gp_m <= gp_ref:
        raise ValueError(f"M gross profit {gp_m:.1f} must exceed the k fitting point's {gp_ref:.1f}")

    # Marginal conversion rate: how much of one extra yuan of gross profit
    # reaches the parent.  Solved from two same-basis measured points, so it
    # carries tax, opex leverage and the minority share without modelling them.
    k = (inp["np_m"] - inp["np_ref"]) / (gp_m - gp_ref)

    step = inp["vol_al"] * _VOL_PRICE_TO_YI
    gp_l = gp_m - step * (inp["spread_m"] - inp["spread_l"])
    gp_h = gp_m + step * (inp["spread_h"] - inp["spread_m"])
    np_l = inp["np_m"] + (gp_l - gp_m) * k
    np_h = inp["np_m"] + (gp_h - gp_m) * k

    return {
        "gp": (gp_l, gp_m, gp_h),
        "np": (np_l, inp["np_m"], np_h),
        "k": k,
    }


def anchors(inp: dict[str, float], fx: float = 1.0) -> tuple[list[float], list[float]]:
    """(anchors in quote currency, EPS in reporting currency).

    `fx` converts CNY earnings to the quote currency: 1.0 for the A shares,
    HKD/CNY for 1378.HK.
    """
    nps = bridge(inp)["np"]
    eps = [np_ / inp["shares"] for np_ in nps]
    return [e * inp["pe"] / fx for e in eps], eps


def check_regression(
    inp: dict[str, float], gp_m_expected: float, np_m_expected: float, tol: float = 0.01
) -> list[str]:
    """M tier must reproduce the filing's annualised gross profit and net profit.

    This is the weld between the bridge and the audited numbers: M is defined as
    "the current regime, made permanent", so if it no longer lands on the
    interim report x2, some input has drifted.
    """
    errors: list[str] = []
    gp_m = inp["rev_m"] * inp["gm_m"]
    if abs(gp_m - gp_m_expected) > gp_m_expected * tol:
        errors.append(f"M gross profit {gp_m:.1f}亿 vs filing {gp_m_expected:.1f}亿")
    if abs(inp["np_m"] - np_m_expected) > np_m_expected * tol:
        errors.append(f"M net profit {inp['np_m']:.1f}亿 vs filing {np_m_expected:.1f}亿")
    return errors


def flat_ladder(eps: list[float]) -> list[str]:
    """A2e-2c: the tiers must already be a ladder in the *numerator*.

    These tickers use a flat multiple, so the flat-口径 restatement is the main
    one — but the check still has to be run, because a tier propped up by
    anything other than its own earnings would show up here as a collapse.
    """
    if all(a < b for a, b in zip(eps, eps[1:])):
        return []
    return [f"EPS ladder violated under flat multiple: {eps}"]


def pb_floor(inp: dict[str, float], scenario_l: float) -> tuple[float, list[str]]:
    """A2e-1 floor check: (floor, errors).

    The asset floor is the lowest PB the stock has actually printed times
    today's book.  Every framework on this axis states that the earnings
    scenario wins at L; if the floor ever overtakes it, the earnings ruler has
    broken down at L and the framework must be restated in asset terms, not
    silently kept.
    """
    floor = inp["pb_low"] * inp["bps"]
    if floor >= scenario_l:
        return floor, [
            f"PB floor {floor:.2f} >= scenario L {scenario_l:.2f} — "
            "L must be restated in asset terms (A2e-1)"
        ]
    return floor, []
