"""Perpetual-growth multiples with reinvestment.

The repo prices a business as `PE = 1/(r - g)` with `r` set by the leg's basket
(skill sediment-framework, references/valuation-rules.md「§r 篮子」), so a
no-growth business is worth `1/r` (10x in the 常规经营 basket).  That form silently assumes
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

# valuation-rules.md「§r 篮子」: r is set by the nature of the business the leg
# belongs to -- seven baskets, repo-level, never a per-ticker dial.  A framework
# names the basket and the reason; the value lives here so the same kind of
# business discounts the same way everywhere and cross-ticker bands stay
# comparable.  Changing a basket value is a repo-level decision taken on two
# quarters of basket-level implied-r evidence, never inside one ticker.  The
# grounding is standard corporate finance: a single company-wide rate overvalues
# the risky leg and undervalues the safe one (Brealey/Myers/Allen, "Company and
# Project Costs of Capital").
BASKET_R: dict[int, tuple[str, float]] = {
    1: ("固定合约年金", 0.065),
    2: ("管制公用事业", 0.075),
    3: ("按量专利许可", 0.085),
    4: ("常规经营", 0.100),
    5: ("大行 / 优质股份行", 0.110),
    6: ("周期商品 / 氟化工", 0.130),
    7: ("单押 / 国别重 / 故事股", 0.150),
}
BASKET_VALUES: tuple[float, ...] = tuple(r for _, r in BASKET_R.values())


def basket_r(basket: int) -> float:
    return BASKET_R[basket][1]


def check_r(r: float, where: str, basket: int | None = None) -> list[str]:
    """A leg's r must be a basket value; with the basket named it must be THAT basket's."""
    if basket is not None:
        if basket not in BASKET_R:
            return [f"{where}: 篮 {basket} is not one of 1..7 (valuation-rules.md「§r 篮子」)"]
        if abs(r - basket_r(basket)) > 1e-9:
            return [f"{where}: r={r} but 篮 {basket} {BASKET_R[basket][0]} is {basket_r(basket)} (valuation-rules.md「§r 篮子」)"]
        return []
    if not any(abs(r - v) < 1e-9 for v in BASKET_VALUES):
        return [f"{where}: r={r} not in baskets {BASKET_VALUES} (valuation-rules.md「§r 篮子」)"]
    return []


# valuation-rules.md「篮子对」: a leg may name two baskets -- what the business
# is today (the journey) and what it becomes in the steady state (the terminal).
# Both are basket values, so this widens the schema without reopening the dial
# that 硬约束 14 closes.  Only a leg whose high rate means "not yet proven" may
# fade; a commodity leg's rate is a permanent property and stays paired to
# itself.  That judgement is evidence, not arithmetic, so it lives in the
# framework's r row -- what is mechanical, and checked here, is that the pair
# only ever fades and that the haircut carrying the arrival risk was declared.
# The steady state does not have to be reached today: not having arrived is
# what k prices, and gating the terminal rate on arrival would charge the
# story-stock rate to the steady state after all.
def check_basket_pair(r_journey: float, r_terminal: float, where: str,
                      basket_journey: int | None = None,
                      basket_terminal: int | None = None) -> list[str]:
    """Both rates are baskets and the pair only fades."""
    errs = check_r(r_journey, f"{where} r_journey", basket_journey)
    errs += check_r(r_terminal, f"{where} r_terminal", basket_terminal)
    if errs:
        return errs
    if r_terminal > r_journey + 1e-9:
        return [f"{where}: r_terminal={r_terminal} above r_journey={r_journey} — a pair prices a "
                f"business that settles down, never one that gets riskier forever "
                f"(valuation-rules.md「篮子对」)"]
    return []


def check_k_declared(r_journey: float, r_terminal: float, ks, where: str) -> list[str]:
    """A faded terminal MUST name the haircut that carries the risk of never arriving."""
    if r_terminal >= r_journey - 1e-9:
        return []
    missing = [tier for tier, k in zip(("L", "M", "H"), ks) if k is None]
    if missing:
        return [f"{where}: r_terminal {r_terminal} < r_journey {r_journey} but k is missing for "
                f"{'/'.join(missing)} — the transition is too short to price arrival risk, so it "
                f"MUST sit in the numerator (valuation-rules.md「篮子对」k 强制)"]
    return []


# valuation-rules.md「永续增长口径」: a perpetual g above the cap is a dial, not a
# state.  The cap keeps at least 4pp between r and g (between r - g = 2% and 4%
# the multiple's elasticity to g passes 25x; 1pp of g moved MRVL.US's M by ~30%)
# and never exceeds the 6% macro ceiling however high r is.  Growth beyond it
# has to be named as a finite transition -- two_stage_pe with n bounded by the
# years a consensus exists for -- and never left in the perpetuity.
G_CAP_MACRO = 0.06
MIN_R_MINUS_G = 0.04

# valuation-rules.md「巨型硬件上限」(2026-09-27): 6% is ~4% long-run nominal
# growth plus a post-horizon runway.  A hardware leg of a company whose
# numerator-year revenue is already >= MEGA_HW_REVENUE_USD (~0.5% of US nominal
# GDP) and that has not proven pricing power gets no runway in the base case:
# its perpetual g stops at the USD r_f anchor.  Revenue, not market cap, so the
# classification never moves with the price.
G_CAP_MEGA_HW = 0.04
MEGA_HW_REVENUE_USD = 1.5e11


def g_cap(r: float, mega_hw: bool = False) -> float:
    ceiling = G_CAP_MEGA_HW if mega_hw else G_CAP_MACRO
    return min(ceiling, r - MIN_R_MINUS_G)


def check_g_cap(g: float, r: float, where: str, mega_hw: bool = False) -> list[str]:
    """A perpetual g must sit at or below g_cap(r); the excess belongs in g1."""
    cap = g_cap(r, mega_hw)
    if g > cap + 1e-9:
        rule = "「巨型硬件上限」— the excess belongs in the candidate band" if mega_hw \
            else "「永续增长口径」— move the excess into a named g1"
        return [f"{where}: g2={g:.4f} > cap {cap:.3f} at r={r} (valuation-rules.md{rule})"]
    return []


def check_transition_years(n: int, consensus_years: int, where: str) -> list[str]:
    """Stage 1 may run only as long as a consensus exists for each year."""
    if n > consensus_years:
        return [f"{where}: n_stage={n} > consensus years {consensus_years} — a transition year without a consensus is a story, not an input"]
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


def capped_payout_pe(g_raw: float, roe: float, r: float, payout_observed: float) -> float:
    """Multiple for a dividend name whose policy g exceeds g_cap(r).

    Gordon credits exactly g/ROE of retention; when g is clipped to the cap the
    company keeps retaining for the uncapped g, and that extra retention is
    neither credited (cap) nor returned (Gordon's implied payout) -- the
    multiple must then be the dividend ruler at the observed payout
    (valuation-rules.md「永续增长口径」capped-g rule, 2026-09-15).  Below the cap
    this is exactly perpetual_pe.
    """
    cap = g_cap(r)
    if g_raw <= cap + 1e-9:
        return perpetual_pe(g_raw, roe, r)
    return payout_observed / (r - cap)


def annuity_factor(g: float, r: float, n: float) -> float:
    """PV of n years of a stream growing at g, discounted at r, per unit of NI_1.

    The finite-life twin of `1/(r - g)`, sharing the basket r.  Use it
    wherever the cash flow has a named end and Gordon's perpetual premise
    therefore fails: a concession that reverts to the host state, a supply
    contract with a stated term.  `n → ∞` recovers `1/(r - g)` for g < r, and
    unlike the perpetual form this stays finite for g > r -- a stream that
    outgrows the discount rate for a bounded number of years is not divergent.

    Returns a PURE PV factor with no retention haircut, so it is not the
    annuity analogue of `perpetual_pe`.  Callers either run it at g = 0 (a
    stream that funds no growth retains nothing) or convert to an equivalent
    perpetual g and route that through `perpetual_pe`, which is where the
    payout term belongs.
    """
    if r == g:
        raise ValueError(f"annuity undefined at r == g == {r}")
    return (1.0 - ((1.0 + g) / (1.0 + r)) ** n) / (r - g)


def justified_pb(g: float, roe: float, r: float) -> float:
    """Clean-surplus P/B on current book: (ROE - g) / (r - g).

    Equivalent to `roe * perpetual_pe(g, roe, r)` when `g > 0` (payout below the
    100% cap).  Banks and other book-anchored rulers call this directly so the
    multiple is stated in PB space rather than PE × ROE.
    """
    if roe <= 0:
        raise ValueError(f"roe must be positive, got {roe}")
    if r - g <= 0:
        raise ValueError(f"perpetuity diverges: g={g} >= r={r}")
    if roe <= g:
        raise ValueError(f"roe={roe} must exceed g={g} for a finite P/B")
    return (roe - g) / (r - g)


def two_stage_pe(g1: float, n: int, g2: float, roe: float, r: float,
                 r_terminal: float | None = None) -> float:
    """Multiple on NEXT-period earnings: n years of transition growth g1 after
    them, then perpetual g2.

    Same valuation date as `perpetual_pe`: today is t=0, the numerator year is
    t=1, and the transition runs E1 -> E2 -> ... -> E_{n+1}, after which the
    Gordon tail applies.  `two_stage_pe(g1, 0, g2, roe, r) == perpetual_pe(g2,
    roe, r)` exactly, so a tier with and a tier without a transition price the
    same numerator year on the same date.  (Until 2026-09-14 this function took
    the numerator as CURRENT earnings at t=0, which put transition tiers one
    year later than the `perpetual_pe` tiers beside them -- valuation-rules
    「两段式过渡」records the switch.)

    Stage-1 payout is deliberately NOT floored at zero.  A business growing
    faster than its ROE funds the gap externally, and `1 - g1/roe` going
    negative is the model charging for that issuance rather than a defect --
    it is the only way this form can express a company that has stopped
    buying back stock and is selling it instead.  Stage 2 keeps the payout
    cap, since no perpetual state hands back more than it earns.

    `r_terminal` splits the two jobs this rate silently does at once
    (valuation-rules.md「篮子对」).  `r` prices the journey -- stage-1 cash and
    the n years the tail is discounted back over -- while `r_terminal`
    capitalises the steady state itself.  A leg whose high `r` says "not yet
    proven" rather than "permanently cyclical" reaches a steady state that
    belongs in a calmer basket, and charging the story-stock rate to a
    perpetuity asserts the business never matures.  Default None keeps `r` in
    both jobs, which is every leg that has not declared a pair.

    The split is worth little on its own: `n` is capped at the consensus years
    (`check_transition_years`), so discounting the tail back at 15% instead of
    10% over one year moves BE.US by 4.3%.  That is why the rule pairs it with
    a mandatory numerator haircut -- the risk of never arriving has no room to
    live in an exponent this small, and must be priced where it is visible.
    At n=0 the exponent is gone entirely and `r` has no arithmetic role; the
    pair still holds, with the haircut carrying all of the arrival risk.
    Keeping `r` on the tail there would charge the story-stock rate to the
    steady state -- the exact error the pair exists to remove.
    """
    if roe <= 0:
        raise ValueError(f"roe must be positive, got {roe}")
    r_tv = r if r_terminal is None else r_terminal
    if r_tv - g2 <= 0:
        raise ValueError(f"perpetuity diverges: g2={g2} >= r_terminal={r_tv}")
    p1 = 1.0 - g1 / roe
    p2 = payout(g2, roe)
    # Dividends of E1..E_n fund the next year's g1; E_{n+1} onward is the tail,
    # valued at t=n and discounted from there.
    stage1 = p1 * _stream_pv(g1, r, n)
    tv = ((1.0 + g1) ** n * p2) / ((r_tv - g2) * (1.0 + r) ** n)
    return stage1 + tv


def _stream_pv(g: float, r: float, n: float) -> float:
    """PV at t=0 of n payments 1, (1+g), ... starting at t=1 — `annuity_factor`
    with the r == g limit filled in, so measured (fractional) n and g1 == r both
    work."""
    if abs(r - g) < 1e-12:
        return n / (1.0 + r)
    return annuity_factor(g, r, n)


def recovery_two_stage_pe(g1: float, n: int, g2: float, roe_mid: float, r: float) -> float:
    """Multiple on a TROUGH year priced as next-period earnings: n years of
    measured recovery after it, then mid-cycle Gordon.

    valuation-rules.md「谷底档」.  Pairing a trough numerator with the steady g2 prices
    the trough as permanent (the cycle counted twice); pairing it with a Gordon
    recovery g fails the other way, because `1 - g/ROE` at a trough ROE below g
    charges the recovery as if it were funded by retained earnings.  A cyclical
    recovery is utilisation and price coming back, not capex, so stage 1 pays
    out 100% (no retention haircut) and only the terminal stage carries the
    payout term -- at the mid-cycle ROE, not the trough one.

    g1 and n are the ticker's own realised trough-to-mid-cycle path (a fact in
    its filings, not a forecast), so `check_transition_years` does not apply.
    Same date convention as `two_stage_pe` / `perpetual_pe`: the trough year is
    t=1, recovery runs E1 -> E_{n+1}, the tail is valued at t=n.
    """
    if n < 1:
        raise ValueError(f"recovery needs at least one year, got n={n}")
    if roe_mid <= 0:
        raise ValueError(f"roe_mid must be positive, got {roe_mid}")
    if r - g2 <= 0:
        raise ValueError(f"perpetuity diverges: g2={g2} >= r={r}")
    stage1 = _stream_pv(g1, r, n)
    tv = ((1.0 + g1) ** n * payout(g2, roe_mid)) / ((r - g2) * (1.0 + r) ** n)
    return stage1 + tv


def implied_g(pe: float, roe: float, r: float) -> tuple[float, bool]:
    """Invert a multiple back into the growth it is paying for.

    Returns (g, well_conditioned).  For a multiple at or below `1/r` the payout
    cap binds and the answer is the plain `r - 1/pe`; above it the retention
    term applies.  The flag is False when ROE sits near r, where the price
    carries almost no information about g and the number should be reported as
    an ambiguity rather than a reading.

    Below `1/r` this is only ONE of two readings -- see `implied_g_retaining`
    and `check_branch`; which one describes the company is decided by its
    observed payout, not by the formula.

    Above `1/r` with ROE < r the multiple is unreachable for any g (the hump
    peaks at 1/r), and the retention formula returns a negative g whose payout
    exceeds 100% -- that is not a reading, so the flag is False there
    (1898.HK's 兖煤澳 peer: 12.62x on ROE 3.3% came back as g -1.48%, payout
    145%, and was fed into a cluster median).
    """
    naive = r - 1.0 / pe
    if naive <= 0.0:
        return naive, True
    g = (1.0 - pe * r) / (1.0 / roe - pe)
    conditioned = abs(roe - r) > _ROE_NEAR_R and 0.0 <= g < min(r, roe)
    return g, conditioned


def implied_g_retaining(pe: float, roe: float, r: float) -> tuple[float, bool]:
    """Invert a multiple on the RETENTION branch, whatever side of 1/r it sits.

    With ROE < r the Gordon multiple is a hump: it peaks at 1/r when g = 0 and
    falls on both sides, so every multiple below 1/r has two readings -- a
    shrinking business paying out 100% (what `implied_g` returns) or a growing
    one funding g by retaining g/ROE.  The formula cannot tell them apart; the
    payout ratio can.  Use this one when the company observably retains
    (1766.HK: 50% payout, DPS CAGR +6%), where the 100%-payout reading
    describes a different company.  Same conditioning flag as `implied_g`,
    plus g must be non-negative for the retention story to hold at all.
    """
    g = (1.0 - pe * r) / (1.0 / roe - pe)
    conditioned = abs(roe - r) > _ROE_NEAR_R and 0.0 <= g < min(r, roe)
    return g, conditioned


# Branch guard (valuation-rules.md「永续增长口径」反解分支).  Two tests:
#
#   sign      -- always.  g2 <= 0 is the full-payout branch; on a company that
#                observably retains it describes a different company.  g2 > 0
#                on a company paying out ~everything funds growth from nothing.
#   magnitude -- only while ROE < r + _ROE_NEAR_R.  There the multiple is a
#                hump and the payout g2 requires (1 - g2/ROE) is a real reading:
#                the two branches sit ~50pp apart (1766.HK: modelled 100% vs
#                paid 50%).  On a high-ROE book every g2 <= g_cap(r) needs >= ~85%
#                payout, so the same gap measures book-vs-marginal ROE
#                (600011.SH: 永续债 lifts common-equity ROE to 16%; GLW.US auto
#                leg on a 40% company ROE) -- report it with `payout_gap`, do
#                not fail on it.
NEAR_FULL_PAYOUT = 0.80   # below this the company observably retains
FULL_PAYOUT = 0.95        # at/above this there is nothing left to fund g > 0
PAYOUT_BRANCH_TOL = 0.20


def payout_gap(g2: float, roe: float, payout_observed: float) -> tuple[float, float]:
    """(modelled payout at g2, modelled - observed) -- the disclosure behind `check_branch`."""
    modelled = payout(g2, roe)
    return modelled, modelled - payout_observed


def check_branch(g2: float, roe: float, r: float, payout_observed: float, where: str) -> list[str]:
    """Wrong-branch red lines; see the block comment above for what each test means."""
    tag = "(valuation-rules.md「反解分支」)"
    if g2 <= 0.0 and payout_observed < NEAR_FULL_PAYOUT:
        return [f"{where}: g2={g2:+.2%} is the full-payout branch but the company pays out "
                f"{payout_observed:.0%} and retains — wrong Gordon branch {tag}"]
    if g2 > 0.0 and payout_observed >= FULL_PAYOUT:
        return [f"{where}: g2={g2:+.2%} is the retention branch but the company pays out "
                f"{payout_observed:.0%} — nothing retained to fund g {tag}"]
    if roe < r + _ROE_NEAR_R:
        modelled, gap = payout_gap(g2, roe, payout_observed)
        if abs(gap) > PAYOUT_BRANCH_TOL:
            return [f"{where}: g2={g2:+.2%} needs payout {modelled:.0%} at ROE {roe:.1%} but the company "
                    f"pays {payout_observed:.0%} — g2 inconsistent with the payout policy {tag}"]
    return []
