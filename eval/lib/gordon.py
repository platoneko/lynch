"""Perpetual-growth multiples with reinvestment.

The repo prices a business as `PE = 1/(r - g)` with `r = 10%`, so a no-growth
business is worth 10x (AGENTS.md「永续增长口径」).  That form silently assumes
a 100% payout — growth arriving free of reinvestment, i.e. an infinite return
on incremental capital.  It overstates the multiple for any `g > 0`, and
inverting it therefore understates the growth a market price is paying for.

The honest form ties growth to the capital it consumes:

    b  = g / ROE                      retention ratio
    PE = (1 - b) / (r - g)

`g = 0` gives `b = 0` and `PE = 1/r` exactly, so the repo's anchor survives
untouched; the two forms only diverge above zero growth, and they diverge more
the lower the ROE.  At `ROE = r` growth is value-neutral — the multiple sits at
`1/r` whatever `g` is — which is a fact about the business, not a defect.

Payout is capped at 100%: a shrinking business is not assumed to hand back more
than it earns, so for `g <= 0` this collapses to `1/(r - g)`.
"""
from __future__ import annotations

# When ROE sits within this of r, growth is close to value-neutral: the
# multiple barely moves with g until it runs into the pole at g = r, and then
# a fraction of a turn of PE swings g by points.  Inverting a price for g in
# that regime produces a number, not a reading.
_ROE_NEAR_R = 0.03

# AGENTS.md「永续增长口径」: r is picked by the leg's cash-flow nature, never by
# ticker, so the same kind of cash flow discounts the same way everywhere and the
# cross-ticker bands stay comparable.  Three tiers, nothing in between -- a free
# dial would make r a price knob, which is exactly what the old flat 10% guarded
# against.  The grounding is standard corporate finance, not a repo invention:
# a single company-wide rate overvalues the risky leg and undervalues the safe one
# (Brealey/Myers/Allen, "Company and Project Costs of Capital").
R_CONTRACTED = 0.07  # payer, rate and term fixed in a signed contract
R_OPERATING = 0.10  # default
R_CONDITIONAL = 0.13  # revenue contingent on unclosed financing or unsigned customers
R_TIERS = (R_CONTRACTED, R_OPERATING, R_CONDITIONAL)


def check_r(r: float, where: str) -> list[str]:
    """A leg's discount rate must be one of the three tiers, not a free knob."""
    if not any(abs(r - tier) < 1e-9 for tier in R_TIERS):
        return [f"{where}: r={r} not in {R_TIERS} (AGENTS.md「永续增长口径」)"]
    return []


def payout(g: float, roe: float) -> float:
    """Fraction of earnings distributable while funding growth g, capped at 1."""
    if roe <= 0:
        raise ValueError(f"roe must be positive, got {roe}")
    return min(1.0, 1.0 - g / roe)


def perpetual_pe(g: float, roe: float, r: float) -> float:
    """Gordon multiple on next-period earnings, net of the reinvestment g needs."""
    if r - g <= 0:
        raise ValueError(f"perpetuity diverges: g={g} >= r={r}")
    return payout(g, roe) / (r - g)


def two_stage_pe(g1: float, n: int, g2: float, roe: float, r: float) -> float:
    """Multiple for n years of transition growth g1, then perpetual g2.

    Stage-1 payout is deliberately NOT floored at zero.  A business growing
    faster than its ROE funds the gap externally, and `1 - g1/roe` going
    negative is the model charging for that issuance rather than a defect --
    it is the only way this form can express a company that has stopped
    buying back stock and is selling it instead.  Stage 2 keeps the payout
    cap, since no perpetual state hands back more than it earns.

    Base note: the transition compounds off the earnings handed in, so this
    returns a multiple on *current* earnings, while `perpetual_pe` returns one
    on next-period earnings.  The two therefore differ by (1 + g2) at n = 0 --
    a tier that runs no transition MUST call `perpetual_pe` directly rather
    than this with n = 0, or it silently prices itself one period later than
    the tiers that do.
    """
    if roe <= 0:
        raise ValueError(f"roe must be positive, got {roe}")
    if r - g2 <= 0:
        raise ValueError(f"perpetuity diverges: g2={g2} >= r={r}")
    p1 = 1.0 - g1 / roe
    p2 = payout(g2, roe)
    stage1 = sum(p1 * ((1.0 + g1) / (1.0 + r)) ** t for t in range(1, n + 1))
    tv = ((1.0 + g1) ** n * (1.0 + g2) * p2) / ((r - g2) * (1.0 + r) ** n)
    return stage1 + tv


def implied_g(pe: float, roe: float, r: float) -> tuple[float, bool]:
    """Invert a multiple back into the growth it is paying for.

    Returns (g, well_conditioned).  For a multiple at or below `1/r` the payout
    cap binds and the answer is the plain `r - 1/pe`; above it the retention
    term applies.  The flag is False when ROE sits near r, where the price
    carries almost no information about g and the number should be reported as
    an ambiguity rather than a reading.
    """
    naive = r - 1.0 / pe
    if naive <= 0.0:
        return naive, True
    g = (1.0 - pe * r) / (1.0 / roe - pe)
    conditioned = abs(roe - r) > _ROE_NEAR_R and g < min(r, roe)
    return g, conditioned
