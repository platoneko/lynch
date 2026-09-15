"""AGENTS.md「谷底档」: measured recovery at full payout, then mid-cycle Gordon."""
from __future__ import annotations

import unittest

from eval.lib.gordon import perpetual_pe, recovery_two_stage_pe, two_stage_pe


class RecoveryTwoStageTests(unittest.TestCase):
    def test_zero_growth_full_payout_is_one_over_r(self) -> None:
        # A flat stream paid out in full is a plain perpetuity whatever n is.
        self.assertAlmostEqual(recovery_two_stage_pe(0.0, 3, 0.0, 0.15, 0.10), 10.0, places=9)

    def test_matches_two_stage_when_retention_is_free(self) -> None:
        # With ROE -> inf the retention haircut vanishes in both stages, so the
        # recovery form and the funded form must agree.
        big_roe = 1e9
        self.assertAlmostEqual(
            recovery_two_stage_pe(0.05, 3, 0.02, big_roe, 0.10),
            two_stage_pe(0.05, 3, 0.02, big_roe, 0.10),
            places=9,
        )

    def test_stage_one_ignores_trough_roe(self) -> None:
        # The trough ROE never enters: only the mid-cycle ROE prices the tail.
        a = recovery_two_stage_pe(0.05, 3, 0.02, 0.12, 0.10)
        b = two_stage_pe(0.05, 3, 0.02, 0.12, 0.10)
        self.assertGreater(a, b)  # funded form charges 1 - g1/ROE in stage 1

    def test_terminal_uses_mid_cycle_gordon(self) -> None:
        # Trough year at t=1 paid in full through the n recovery years; the
        # tail on E_{n+1} is perpetual_pe at the mid ROE, valued at t=n.
        g1, n, g2, roe, r = 0.05, 3, 0.02, 0.12, 0.10
        stage1 = sum((1 + g1) ** (t - 1) / (1 + r) ** t for t in range(1, n + 1))
        tail = (1 + g1) ** n / (1 + r) ** n * perpetual_pe(g2, roe, r)
        self.assertAlmostEqual(recovery_two_stage_pe(g1, n, g2, roe, r), stage1 + tail, places=9)

    def test_two_stage_with_no_transition_is_perpetual(self) -> None:
        # Same valuation date as perpetual_pe: n=0 collapses to it exactly, so a
        # tier with a transition and one without price the same numerator year
        # on the same day (valuation-rules「两段式过渡」, 2026-09-14).
        self.assertAlmostEqual(two_stage_pe(0.5, 0, 0.06, 0.886, 0.10), perpetual_pe(0.06, 0.886, 0.10), places=12)

    def test_two_stage_numerator_is_next_period(self) -> None:
        # One transition year: E1 pays 1 - g1/ROE at t=1, the tail on E2 = E1(1+g1)
        # is valued at t=1 and discounted once.
        g1, g2, roe, r = 0.9063, 0.06, 0.886, 0.10
        expect = ((1 - g1 / roe) + (1 + g1) * perpetual_pe(g2, roe, r)) / (1 + r)
        self.assertAlmostEqual(two_stage_pe(g1, 1, g2, roe, r), expect, places=12)

    def test_rejects_zero_years(self) -> None:
        with self.assertRaises(ValueError):
            recovery_two_stage_pe(0.05, 0, 0.02, 0.12, 0.10)


if __name__ == "__main__":
    unittest.main()
