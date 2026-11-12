"""Synthetic-only tests; no stock model or historical data is loaded."""
import unittest

from eval.lib import valuation_guards as guards


class SyntheticValuationGuardTests(unittest.TestCase):
    def test_alignment_accepts_matching_synthetic_year(self):
        self.assertIsNone(guards.require_axis_alignment({'t_align': 2044}, 2044))

    def test_alignment_rejects_changed_missing_or_fractional_year(self):
        for value in ({'t_align': 2045}, {}, {'t_align': 2044.5}, {'t_align': True}):
            with self.subTest(axis=value), self.assertRaisesRegex(ValueError, 'align'):
                guards.require_axis_alignment(value, 2044)

    def test_pending_evidence_or_nonpositive_increment_requires_review(self):
        for increment, overlap, path in ((10.0, False, True), (10.0, True, False),
                                         (0.0, True, True), (-1.0, True, True)):
            with self.subTest(increment=increment, overlap=overlap, path=path):
                self.assertEqual(guards.option_reconciliation_status(increment, overlap, path),
                                 'requires_reconciliation')

    def test_resolved_synthetic_case_remains_conditional(self):
        self.assertEqual(guards.option_reconciliation_status(10.0, True, True), 'conditional')

    def test_status_rejects_untyped_or_nonfinite_observations(self):
        for increment, overlap, path in ((float('nan'), True, True), (True, True, True),
                                         (1.0, 1, True), (1.0, True, None)):
            with self.subTest(increment=increment, overlap=overlap, path=path), self.assertRaises(ValueError):
                guards.option_reconciliation_status(increment, overlap, path)


if __name__ == '__main__':
    unittest.main()
