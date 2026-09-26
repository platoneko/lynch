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

The multiple is generated, not typed (valuation-rules.md「永续增长口径」).  Each
tier names a perpetual g written once in the axis file (g_L / g_M / g_H —
currently all 0%: the axis L rung 4,250 is a GS 2027E supply-glut *level*
that may persist, not a trough that mean-reverts; IAI 3.3% is sensitivity /
stretched only) and the tier's forward ROE = NP_x / equity turns it into
`perpetual_pe`.  The L world is the axis's L rung — a nameable regime
(「低位 regime 持续」), not the ticker's own trough year — so it takes the
steady g on ROE_L (valuation-rules「谷底档」适用范围); the 谷底档's recovery stage
(`recovery_two_stage_pe`) applies only to an own realised trough year, and
running it on an axis rung lets L converge on M.  Each ticker's own printed
trough × 1/r is only the deep_discount pressure coordinate.  The ticker's own
realised PE band is only a guardrail: a generated multiple outside it is
answered by reverse-solving that tier's g to the nearest band edge (裁决顺序
③), and a band no legal g can reach stops the ticker rather than being clipped.

Only the per-ticker inputs differ, so the arithmetic lives here rather than in
four copies.  What does not live here is each ticker's own guard — Chalco's
minority drain, Tianshan's volume basis, Hongqiao's integration shape —
because those are the parts that are genuinely not shared.
"""

from __future__ import annotations

from .axes import AXIS_AL, check_axis_g_mount
from .gordon import check_g_cap, check_r, perpetual_pe

REQUIRED = [
    "shares",
    "rev_m", "gm_m", "rev_ref", "gm_ref", "np_m", "np_ref",
    "vol_al", "spread_l", "spread_m", "spread_h",
    "equity", "r_perp", "g2_l", "g2_m", "g2_h", "pe_own_min", "pe_own_max",
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


def multiples(inp: dict[str, float]) -> tuple[list[float], list[float]]:
    """(PE per tier, forward ROE per tier), both generated from the Inputs.

    ROE_x = NP_x / equity is the tier's own return on today's book, so the
    retention Gordon charges for g_x is the tier's, not a company average.
    """
    nps = bridge(inp)["np"]
    roes = [np_ / inp["equity"] for np_ in nps]
    r = inp["r_perp"]
    return [
        perpetual_pe(inp[f"g2_{tag}"], roe, r) for tag, roe in zip("lmh", roes)
    ], roes


def anchors(inp: dict[str, float], fx: float = 1.0) -> tuple[list[float], list[float]]:
    """(anchors in quote currency, EPS in reporting currency).

    `fx` converts CNY earnings to the quote currency: 1.0 for the A shares,
    HKD/CNY for 1378.HK.
    """
    nps = bridge(inp)["np"]
    eps = [np_ / inp["shares"] for np_ in nps]
    pes, _ = multiples(inp)
    return [e * pe / fx for e, pe in zip(eps, pes)], eps


def check_gordon(inp: dict[str, float], basket: int | None = None) -> list[str]:
    """Everything valuation-rules.md asks of a generated multiple, in one place.

    r in its basket; every g under g_cap(r); the tier shape flat-or-reverse and
    equal to the axis's shared g rows; and the guardrail — each generated PE
    inside the band this stock has actually printed.  A guardrail miss is
    reported, never clipped: widening the band or bending g to pass is the
    exact move the rule exists to forbid.
    """
    r = inp["r_perp"]
    errors = check_r(r, "r_perp", basket)
    stated = {"L": inp["g2_l"], "M": inp["g2_m"], "H": inp["g2_h"]}
    for tag, g in stated.items():
        errors += check_g_cap(g, r, f"g2_{tag.lower()}")
    errors += check_axis_g_mount(AXIS_AL, stated)
    if errors:
        return errors

    pes, _ = multiples(inp)
    if not (pes[0] >= pes[1] >= pes[2]):
        errors.append(
            f"cyclical multiple policy violated: PE {pes[0]:.2f}/{pes[1]:.2f}/{pes[2]:.2f} "
            "must be flat or reverse (skill A2e-5)"
        )
    lo, hi = inp["pe_own_min"], inp["pe_own_max"]
    for tag, pe in zip("LMH", pes):
        if not (lo <= pe <= hi):
            errors.append(
                f"{tag} generated PE {pe:.2f} outside own realised band [{lo}, {hi}] — "
                "reverse-solve that tier's g to the nearest edge (裁决顺序 ③), do not force r or widen the band"
            )
    return errors


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
