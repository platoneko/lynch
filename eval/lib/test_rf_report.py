"""rf_block: the shared r_f readings block (valuation-rules「利率轴」)."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from eval.lib import rf_report

SEG = {"rf_mid": 0.04, "rf_now": 0.0512, "rf_cons": 0.0475,
       "delta_cons": 0.0075, "rf_step": 0.005, "rf_cap": 0.005}


def gordon_band(dr: float) -> tuple[float, float, float]:
    # E / (r - g) with r = 10% + dr, g = 3%, E = 7 / 10 / 13
    return tuple(e / (0.07 + dr) for e in (7.0, 10.0, 13.0))


class RfBlockTests(unittest.TestCase):
    def run_block(self):
        with mock.patch.object(rf_report, "load_rates", return_value=SEG), redirect_stdout(io.StringIO()) as out:
            blk = rf_report.rf_block("USD", {"r_perp": 0.10}, gordon_band)
        return blk, out.getvalue()

    def test_consensus_is_band_at_delta(self) -> None:
        blk, _ = self.run_block()
        self.assertEqual(blk.consensus, gordon_band(0.0075))

    def test_pressure_widens_both_ends_keeps_m(self) -> None:
        blk, _ = self.run_block()
        self.assertEqual(blk.pressure, (gordon_band(0.005)[0], gordon_band(0.0)[1], gordon_band(-0.005)[2]))

    def test_prints_decomposition_and_main_marker(self) -> None:
        _, out = self.run_block()
        self.assertIn("r_perp = 4.00% + 6.00% = 10.00%", out)
        self.assertEqual(out.count("<- 中枢 = 主 zone"), 1)
        self.assertIn("共识 4.75%", out)


if __name__ == "__main__":
    unittest.main()
