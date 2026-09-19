#!/usr/bin/env python3
"""Cross-ticker band calibration: L/M, H/M, neutral width — grouped by Lynch class.

The five zone ids are broadcast side by side by `daily_bands.py`, so they are only
comparable when the three anchors were built the same way. This prints the repo's
current distribution so a framework's ratios can be read against its own class
(see skill sediment-framework references/valuation-rules.md「锚点语义」and A2e).

Reads frameworks/*.md only. Writes nothing.
"""
from __future__ import annotations

import math
import re
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.frameworks import FrameworkMeta, lynch_cn, load_all_frameworks  # noqa: E402
from eval.lib.gordon import basket_r, g_cap  # noqa: E402


_PE_ROW = re.compile(r"^\|\s*(pe_[lmh])\s*\|\s*([0-9.]+)\s*\|", re.MULTILINE)
_GAMMA_ROW = re.compile(r"^\|\s*gamma_pe\s*\|\s*(-?[0-9.]+)\s*\|", re.MULTILINE)
# `g2` or `g2_<leg>` only — `g200_units` is a unit count, not a growth rate.
_G2_ROW = re.compile(r"^\|\s*(g2(?:_[a-z0-9_]+)?)\s*\|\s*(-?[0-9.]+)\s*\|", re.MULTILINE)


def _ratios(m: FrameworkMeta) -> tuple[float, float, float]:
    """L/M, H/M, and the neutral band's width as a share of M — (B−A)/M = (H−L)/2M."""
    return m.L / m.M, m.H / m.M, (m.H - m.L) / (2 * m.M)


def _pe_ratios(m: FrameworkMeta) -> tuple[float, float] | None:
    """pe_L/pe_M and pe_H/pe_M, when the framework exposes a plain pe band.

    L/M is the product of a numerator ratio and this multiple ratio, so the two
    have to be split before any cross-ticker reading (skill A2e-5). Frameworks
    with per-leg multiples or a floor-bound tier expose no single pe_* band and
    are reported without the split rather than with a wrong one.
    """
    pes = dict(_PE_ROW.findall(m.path.read_text(encoding="utf-8")))
    if not {"pe_l", "pe_m", "pe_h"} <= pes.keys():
        return None
    pe_l, pe_m, pe_h = (float(pes[k]) for k in ("pe_l", "pe_m", "pe_h"))
    return pe_l / pe_m, pe_h / pe_m


def _baskets(m: FrameworkMeta) -> str:
    """Which r baskets this framework's legs sit in (valuation-rules.md「§r 篮子」).

    r follows the leg's business, so a ticker with a contracted leg discounts
    part of itself at 6.5% while its neighbours sit at 10%.  That has to be
    visible here, or its L/M gets read as a scenario difference when it is
    really a discount-rate one.  Frameworks on a direct pe band name no basket
    and print a blank.
    """
    baskets = sorted(set(m.baskets.values()))
    return "/".join(str(b) for b in baskets) if baskets else "—"


def _g2_max(m: FrameworkMeta) -> str:
    """Highest perpetual g the framework names, read from its `g2*` inputs.

    valuation-rules.md「永续增长口径」caps the perpetuity at g_cap(r); anything above it is flagged with
    "!" so the dial shows up in the cross-ticker table instead of hiding in
    one file's Inputs.  Frameworks that name their perpetual g under another
    key (`g_sw`, `g_bank`, ...) print a blank, not a zero.
    """
    rows = _G2_ROW.findall(m.path.read_text(encoding="utf-8"))
    if not rows:
        return "—"
    top = max(float(v) for _, v in rows)
    # The cap follows the lowest-r leg: any g2 above it is a dial somewhere.
    cap = g_cap(min(basket_r(b) for b in m.baskets.values())) if m.baskets else g_cap(0.10)
    return f"{top * 100:.1f}{'!' if top > cap + 1e-9 else ''}"


def _gamma_measured(m: FrameworkMeta) -> float | None:
    """The framework's measured d log(PE) / d log(EPS), when it has one.

    Present only where someone actually fitted log P on log EPS for that ticker
    (`600309.SH` A6 is the worked example).  Absent is the common case and is
    printed as a blank, not a zero — no measurement is not the same claim as a
    flat one.
    """
    hit = _GAMMA_ROW.search(m.path.read_text(encoding="utf-8"))
    return float(hit.group(1)) if hit else None


def _gamma_implied(num_ratio: float, pe_ratio: float) -> float | None:
    """The gamma the band itself embeds on one side: ln(pe_x/pe_M) / ln(eps_x/eps_M).

    This is the number to read against a measured gamma — a flat band embeds 0,
    an inverse band a negative, a same-direction band a positive.  It says what
    the author's multiple policy *assumes* about the market, in the same unit
    the data can be measured in.
    """
    if num_ratio <= 0.0 or abs(math.log(num_ratio)) < 1e-9:
        return None
    return math.log(pe_ratio) / math.log(num_ratio)


def main() -> int:
    # BLOCKED frameworks carry a reference band, not a deck; they stay out of the distributions.
    all_metas = load_all_frameworks()
    blocked = [m.ticker for m in all_metas if m.blocked]
    metas = [m for m in all_metas if not m.blocked]

    by_class: dict[str, list[FrameworkMeta]] = {}
    for m in metas:
        by_class.setdefault(m.lynch_primary, []).append(m)

    for primary in sorted(by_class):
        group = sorted(by_class[primary], key=lambda m: _ratios(m)[0])
        lm_median = statistics.median(_ratios(m)[0] for m in group)

        print(f"\n{primary}（{lynch_cn(primary)}）— 同类 L/M 中位 {lm_median:.2f}")
        print(
            f"  {'ticker':12} {'L/M':>6} {'H/M':>6} {'neutral宽/M':>11} {'篮':>6} {'g2%':>6}"
            f"  {'分子比L':>7} {'分子比H':>7}  {'γ隐含L':>7} {'γ隐含H':>7} {'γ实测':>6}  次类"
        )
        for m in group:
            lm, hm, width = _ratios(m)
            pe = _pe_ratios(m)
            # Cross-ticker reading uses the numerator ratio; L/M alone compares
            # the authors' multiple habits as much as their scenarios (A2e-5).
            num_l = f"{lm / pe[0]:7.2f}" if pe else f"{'—':>7}"
            num_h = f"{hm / pe[1]:7.2f}" if pe else f"{'—':>7}"
            # ...and the gammas say whether that multiple policy matches what the
            # market has actually done to this ticker, or only what the author
            # assumed.  A blank γ实测 means nobody has fitted it yet.
            g_l = _gamma_implied(lm / pe[0], pe[0]) if pe else None
            g_h = _gamma_implied(hm / pe[1], pe[1]) if pe else None
            g_meas = _gamma_measured(m)
            fmt = lambda g: f"{g:+7.2f}" if g is not None else f"{'—':>7}"  # noqa: E731
            meas = f"{g_meas:+6.2f}" if g_meas is not None else f"{'—':>6}"
            sub = m.lynch_secondary or "—"
            print(
                f"  {m.ticker:12} {lm:6.2f} {hm:6.2f} {width:10.0%} {_baskets(m):>6} {_g2_max(m):>6}"
                f" {num_l} {num_h}  {fmt(g_l)} {fmt(g_h)} {meas}  {sub}"
            )

    all_lm = [_ratios(m)[0] for m in metas]
    all_hm = [_ratios(m)[1] for m in metas]
    print(
        f"\n全仓 {len(metas)} 票：L/M {min(all_lm):.2f}–{max(all_lm):.2f}"
        f"（中位 {statistics.median(all_lm):.2f}）· "
        f"H/M {min(all_hm):.2f}–{max(all_hm):.2f}（中位 {statistics.median(all_hm):.2f}）"
    )
    if blocked:
        print(f"BLOCKED（不进分布）: {', '.join(blocked)}")
    print("散布 MUST 可归因到基本面或林奇分类，MUST NOT 来自构造手法（valuation-rules.md「锚点语义」）")
    print(
        "γ = dlog(PE)/dlog(EPS)：隐含值是该票倍数政策所假设的（flat=0、反向<0、同向>0），"
        "实测值来自 §3 `gamma_pe`。两者异号或隐含幅度超出实测，说明倍数政策与实证不符"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
