#!/usr/bin/env python3
"""Cross-ticker band calibration: L/M, H/M, neutral width — grouped by Lynch class.

The five zone ids are broadcast side by side by `daily_bands.py`, so they are only
comparable when the three anchors were built the same way. This prints the repo's
current distribution so a framework's ratios can be read against its own class
(see AGENTS.md「锚点语义」and skill sediment-framework A2e).

Reads frameworks/*.md only. Writes nothing.
"""
from __future__ import annotations

import re
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.frameworks import FrameworkMeta, lynch_cn, load_all_frameworks  # noqa: E402


_PE_ROW = re.compile(r"^\|\s*(pe_[lmh])\s*\|\s*([0-9.]+)\s*\|", re.MULTILINE)


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


def main() -> int:
    metas = load_all_frameworks()

    by_class: dict[str, list[FrameworkMeta]] = {}
    for m in metas:
        by_class.setdefault(m.lynch_primary, []).append(m)

    for primary in sorted(by_class):
        group = sorted(by_class[primary], key=lambda m: _ratios(m)[0])
        lm_median = statistics.median(_ratios(m)[0] for m in group)

        print(f"\n{primary}（{lynch_cn(primary)}）— 同类 L/M 中位 {lm_median:.2f}")
        print(
            f"  {'ticker':12} {'L/M':>6} {'H/M':>6} {'neutral宽/M':>11}"
            f"  {'分子比L':>7} {'分子比H':>7}  次类"
        )
        for m in group:
            lm, hm, width = _ratios(m)
            pe = _pe_ratios(m)
            # Cross-ticker reading uses the numerator ratio; L/M alone compares
            # the authors' multiple habits as much as their scenarios (A2e-5).
            num_l = f"{lm / pe[0]:7.2f}" if pe else f"{'—':>7}"
            num_h = f"{hm / pe[1]:7.2f}" if pe else f"{'—':>7}"
            sub = m.lynch_secondary or "—"
            print(
                f"  {m.ticker:12} {lm:6.2f} {hm:6.2f} {width:10.0%}"
                f" {num_l} {num_h}  {sub}"
            )

    all_lm = [_ratios(m)[0] for m in metas]
    all_hm = [_ratios(m)[1] for m in metas]
    print(
        f"\n全仓 {len(metas)} 票：L/M {min(all_lm):.2f}–{max(all_lm):.2f}"
        f"（中位 {statistics.median(all_lm):.2f}）· "
        f"H/M {min(all_hm):.2f}–{max(all_hm):.2f}（中位 {statistics.median(all_hm):.2f}）"
    )
    print("散布 MUST 可归因到基本面或林奇分类，MUST NOT 来自构造手法（AGENTS.md「锚点语义」）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
