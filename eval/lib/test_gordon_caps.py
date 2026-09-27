"""valuation-rules.md「§r 篮子」「永续增长口径」: basket r, g cap, transition years."""
from __future__ import annotations

import unittest

from eval.lib.gordon import (
    BASKET_R,
    MEGA_HW_REVENUE_USD,
    basket_r,
    check_g_cap,
    check_r,
    check_transition_years,
    g_cap,
)


class BasketTests(unittest.TestCase):
    def test_seven_baskets(self) -> None:
        self.assertEqual([basket_r(b) for b in range(1, 8)],
                         [0.065, 0.075, 0.085, 0.10, 0.11, 0.13, 0.15])
        self.assertEqual(BASKET_R[4][0], "龙头制造")

    def test_r_in_table_passes(self) -> None:
        self.assertEqual(check_r(0.10, "r_perp"), [])
        self.assertEqual(check_r(0.10, "r_perp", basket=4), [])

    def test_r_off_table_is_flagged(self) -> None:
        errs = check_r(0.12, "r_perp")
        self.assertEqual(len(errs), 1)
        self.assertIn("r_perp", errs[0])

    def test_r_basket_mismatch_is_flagged(self) -> None:
        errs = check_r(0.13, "r_perp", basket=4)
        self.assertEqual(len(errs), 1)
        self.assertIn("篮 4", errs[0])


class GordonCapTests(unittest.TestCase):
    def test_cap_follows_r(self) -> None:
        self.assertAlmostEqual(g_cap(0.065), 0.025)
        self.assertAlmostEqual(g_cap(0.075), 0.035)
        self.assertAlmostEqual(g_cap(0.10), 0.06)
        self.assertAlmostEqual(g_cap(0.15), 0.06)

    def test_g_at_cap_passes(self) -> None:
        self.assertEqual(check_g_cap(0.06, 0.10, "g2_x"), [])
        self.assertEqual(check_g_cap(0.035, 0.075, "g2_x"), [])

    def test_g_above_cap_is_flagged(self) -> None:
        errs = check_g_cap(0.04, 0.075, "g2_x")
        self.assertEqual(len(errs), 1)
        self.assertIn("g2_x", errs[0])

    def test_mega_hardware_cap_is_four_percent(self) -> None:
        self.assertAlmostEqual(g_cap(0.10, mega_hw=True), 0.04)
        self.assertAlmostEqual(g_cap(0.15, mega_hw=True), 0.04)
        # the r - 4pp spread still binds below it
        self.assertAlmostEqual(g_cap(0.075, mega_hw=True), 0.035)

    def test_mega_hardware_g_above_four_percent_is_flagged(self) -> None:
        self.assertEqual(check_g_cap(0.04, 0.10, "g2", mega_hw=True), [])
        errs = check_g_cap(0.06, 0.10, "g2", mega_hw=True)
        self.assertEqual(len(errs), 1)
        self.assertIn("巨型硬件", errs[0])

    def test_mega_hardware_revenue_threshold(self) -> None:
        self.assertEqual(MEGA_HW_REVENUE_USD, 1.5e11)

    def test_transition_within_consensus_years_passes(self) -> None:
        self.assertEqual(check_transition_years(1, 1, "n_stage"), [])

    def test_transition_beyond_consensus_years_is_flagged(self) -> None:
        errs = check_transition_years(2, 1, "n_stage")
        self.assertEqual(len(errs), 1)
        self.assertIn("n_stage", errs[0])


if __name__ == "__main__":
    unittest.main()
