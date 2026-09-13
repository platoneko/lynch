"""AGENTS.md「永续增长口径」caps: perpetual g and transition years."""
from __future__ import annotations

import unittest

from eval.lib.gordon import G_CAP, check_g_cap, check_transition_years


class GordonCapTests(unittest.TestCase):
    def test_g_at_cap_passes(self) -> None:
        self.assertEqual(check_g_cap(G_CAP, "g2_x"), [])

    def test_g_above_cap_is_flagged(self) -> None:
        errs = check_g_cap(G_CAP + 0.005, "g2_x")
        self.assertEqual(len(errs), 1)
        self.assertIn("g2_x", errs[0])

    def test_transition_within_consensus_years_passes(self) -> None:
        self.assertEqual(check_transition_years(1, 1, "n_stage"), [])

    def test_transition_beyond_consensus_years_is_flagged(self) -> None:
        errs = check_transition_years(2, 1, "n_stage")
        self.assertEqual(len(errs), 1)
        self.assertIn("n_stage", errs[0])


if __name__ == "__main__":
    unittest.main()
