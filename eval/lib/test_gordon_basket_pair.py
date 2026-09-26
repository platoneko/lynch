"""valuation-rules.md「篮子对」: journey basket discounts, terminal basket capitalises."""
from __future__ import annotations

import unittest

from eval.lib.frameworks import FRAMEWORKS_DIR
from eval.lib.gordon import (
    check_basket_pair,
    check_k_declared,
    perpetual_pe,
    two_stage_pe,
)

_NEEDS_CONTENT = unittest.skipUnless(FRAMEWORKS_DIR.is_dir(), "content repo not cloned at frameworks/")

# BE.US M tier, the worked example the rule is written against.
BE = dict(g1=0.6016, n=1, g2=0.06, roe=0.3184)


class PairIsOptIn(unittest.TestCase):
    """A leg that names no pair must price exactly as it did before the rule."""

    def test_default_unchanged(self) -> None:
        self.assertEqual(two_stage_pe(**BE, r=0.15),
                         two_stage_pe(**BE, r=0.15, r_terminal=0.15))

    def test_pair_only_moves_the_tail(self) -> None:
        # 篮 7 journey + 篮 4 terminal sits between the two single-basket reads,
        # nearer the terminal one: the tail is ~100% of the value at n=1.
        paired = two_stage_pe(**BE, r=0.15, r_terminal=0.10)
        self.assertLess(two_stage_pe(**BE, r=0.15), paired)
        self.assertLess(paired, two_stage_pe(**BE, r=0.10))
        self.assertAlmostEqual(paired, 27.4829, places=3)

    def test_transition_still_degenerates_to_perpetual(self) -> None:
        self.assertAlmostEqual(two_stage_pe(0.6016, 0, 0.06, 0.3184, 0.15),
                               perpetual_pe(0.06, 0.3184, 0.15), places=12)


class PairGuards(unittest.TestCase):
    def test_accepts_a_fading_pair(self) -> None:
        self.assertEqual(check_basket_pair(0.15, 0.10, "BE", 7, 4), [])

    def test_rejects_a_rising_pair(self) -> None:
        self.assertIn("never one that gets riskier", check_basket_pair(0.10, 0.15, "X")[0])

    def test_rejects_a_non_basket_rate(self) -> None:
        self.assertIn("not in baskets", check_basket_pair(0.15, 0.12, "X")[0])

    def test_rejects_a_named_basket_that_disagrees(self) -> None:
        self.assertIn("篮 6", check_basket_pair(0.15, 0.10, "X", 7, 6)[0])

    def test_single_stage_capitalises_at_the_terminal_rate(self) -> None:
        # HIMS.US: no transition, so the journey rate has no arithmetic role and
        # k carries all arrival risk -- but the steady state must NOT fall back
        # to the story-stock rate, or the pair would undo itself at n=0.
        self.assertEqual(check_basket_pair(0.15, 0.10, "HIMS", 7, 4), [])
        self.assertAlmostEqual(two_stage_pe(0.6016, 0, 0.06, 0.3184, 0.15, r_terminal=0.10),
                               perpetual_pe(0.06, 0.3184, 0.10), places=12)


class HaircutIsMandatoryWhenFaded(unittest.TestCase):
    def test_missing_k_names_the_tiers(self) -> None:
        err = check_k_declared(0.15, 0.10, (None, 0.4, None), "BE")[0]
        self.assertIn("L/H", err)

    def test_declared_k_passes(self) -> None:
        self.assertEqual(check_k_declared(0.15, 0.10, (0.4, 0.4, 0.4), "BE"), [])

    def test_zero_is_declared_not_missing(self) -> None:
        # k = 0 is a judgement the framework must write a reason for, not a gap.
        self.assertEqual(check_k_declared(0.15, 0.10, (0.0, 0.0, 0.0), "BE"), [])

    def test_unpaired_leg_needs_no_k(self) -> None:
        self.assertEqual(check_k_declared(0.15, 0.15, (None, None, None), "X"), [])


if __name__ == "__main__":
    unittest.main()


@_NEEDS_CONTENT
class LabHaircutIsOneAxisValue(unittest.TestCase):
    """_AXIS.frontier-lab-funding §3b: five sellers, one k -- never five."""

    def test_axis_value_is_readable(self) -> None:
        from eval.lib.axes import load_k_lab
        self.assertTrue(0.0 <= load_k_lab() <= 1.0)

    def test_a_ticker_cannot_carry_its_own_k(self) -> None:
        from eval.lib.axes import check_lab_mount, load_k_lab
        self.assertEqual(check_lab_mount(load_k_lab()), [])
        self.assertIn("never one ticker", check_lab_mount(load_k_lab() + 0.05)[0])
