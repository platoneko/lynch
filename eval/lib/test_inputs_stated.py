"""check_against_stated, and the price-free guard that replaced its zone check."""
from __future__ import annotations

import unittest

from eval.lib.frameworks import price_free_violations
from eval.lib.inputs import check_against_stated

ANCHORS = {"L": 10.0, "M": 20.0, "H": 40.0, "A": 15.0, "B": 30.0}
DECK = "### Outputs\n\n- 底牌 L = **10** · M = **20** · H = **40** · 切点 A = **15** · B = **30**\n"


class StatedAnchorTests(unittest.TestCase):
    def test_matching_anchors_pass(self) -> None:
        self.assertEqual(check_against_stated(DECK, ANCHORS), [])

    def test_anchor_drift_fails(self) -> None:
        self.assertEqual(check_against_stated(DECK, {**ANCHORS, "M": 21.0}),
                         ["M: stated 20.0 vs recomputed 21.0000"])


class PriceFreeTests(unittest.TestCase):
    def test_clean_deck_passes(self) -> None:
        md = "# X.US — x\n\n## 3. 可演算块\n\n| pe_m | 20 | 同业 | 2026-09-11 |\n" + DECK
        self.assertEqual(price_free_violations(md), [])

    def test_each_own_price_form_is_named_with_its_line(self) -> None:
        md = (
            "# X.US — x\n"                                            # 1
            "> **状态：BLOCKED**（③ 出带）\n"                          # 2
            "- 现价 **25** → zone = **`neutral`**（已过 A2f）\n"       # 3
            "| price | 25 | Longbridge 收 | 2026-09-11 |\n"            # 4
            "| ha_ratio | 0.6 | 两地现价之比 | 2026-09-11 |\n"          # 5
        )
        self.assertEqual(sorted(price_free_violations(md)), sorted([
            "line 2: BLOCKED status line (BLOCKED never lands)",
            "line 3: current price figure",
            "line 3: price → zone reading",
            "line 3: stated current zone",
            "line 3: A2f verdict (a research-time check)",
            "line 4: Inputs row of the ticker's own price",
            "line 5: Inputs row of the ticker's own price",
        ]))

    def test_changelog_keeps_its_history(self) -> None:
        md = DECK + "\n## 4. Changelog\n\n### 2026-09-11\n- 现价 25 → zone = **`neutral`**\n"
        self.assertEqual(price_free_violations(md), [])

    def test_other_prices_and_dated_history_pass(self) -> None:
        md = ("| kcl_price_m | 3000 | 钾肥价 | 2026-09-11 |\n"
              "| nav_ref | 1.23 | ETF 净值 | 2026-09-11 |\n"
              "- 2025 年内最低收盘 38.42（2025-04-07）落 `neutral`\n"
              "- 金实现率 = 2026H1 实现价 4,661 ÷ 轴 M\n"
              "- \\(V_{opt}\\) = **28.12** → zone+V ≈ **352.6 / 484.8 / 567.3**\n")
        self.assertEqual(price_free_violations(md), [])


if __name__ == "__main__":
    unittest.main()
