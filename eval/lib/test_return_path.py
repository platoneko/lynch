"""Checks for the present value of a return that fades to maturity."""

import unittest

from eval.lib import gordon


class ReturnPathTest(unittest.TestCase):
    def test_falling_return_path_has_hand_calculated_value(self):
        # 0.9 / 1.1 in the first year; the second year's 1.02 earnings
        # carry an 80% payout into perpetuity and are discounted one year.
        value = gordon.return_path_pe(2026, 2026, 2027, 0.10, 0.02, 0.20, 0.10)
        self.assertAlmostEqual(value, 10.090909090909092)

    def test_negative_growth_never_distributes_more_than_earnings(self):
        value = gordon.return_path_pe(2026, 2026, 2027, 0.10, -0.02, 0.20, 0.10)
        self.assertAlmostEqual(value, 8.333333333333334)

    def test_margin_fades_before_perpetuity(self):
        # Year one margin is 0.5, thereafter 0.25; no growth or retention.
        value = gordon.return_path_pe(2026, 2026, 2027, 0.10, 0.0, 0.20, 0.10,
                                      margin_start=0.5, margin_end=0.25)
        self.assertAlmostEqual(value, 2.727272727272727)

    def test_transition_retains_negative_payout_without_flooring(self):
        # The first year's 30% growth exceeds the 20% return; the model
        # deliberately finances the negative payout during this finite period.
        value = gordon.return_path_pe(2026, 2026, 2027, 0.10, 0.02, 0.20, 0.10,
                                      growth_first=0.30, transition_years=1)
        self.assertAlmostEqual(value, 11.363636363636363)

    def test_return_path_rejects_invalid_boundaries(self):
        for kwargs in (
            {"fade_end": 2026}, {"mature_return": 0.0},
            {"forward_return": 0.0}, {"growth": float("nan")},
            {"transition_years": -1}, {"fade_end": 2027.5},
        ):
            inputs = dict(start_year=2026, fade_start=2026, fade_end=2027,
                          mature_return=0.10, growth=0.02,
                          forward_return=0.20, discount_rate=0.10)
            inputs.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                gordon.return_path_pe(**inputs)

    def test_transition_with_a_changing_margin_requires_explicit_model(self):
        with self.assertRaises(ValueError):
            gordon.return_path_pe(2026, 2026, 2027, 0.10, 0.02, 0.20, 0.10,
                                  growth_first=0.1, transition_years=1,
                                  margin_start=0.5, margin_end=0.25)

    def test_growth_cannot_reach_discount_rate(self):
        with self.assertRaises(ValueError):
            gordon.return_path_pe(2026, 2026, 2027, 0.10, 0.10, 0.20, 0.10)

    def test_perpetual_reinvestment_cannot_consume_all_or_more_earnings(self):
        for growth in (0.05, 0.06):
            with self.subTest(growth=growth), self.assertRaises(ValueError):
                gordon.return_path_pe(2026, 2026, 2027, 0.05, growth, 0.08, 0.10)


if __name__ == "__main__":
    unittest.main()
