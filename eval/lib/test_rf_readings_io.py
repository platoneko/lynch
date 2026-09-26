"""output/rates/<TICKER>.json: eval writes r_f readings, the daily digest reads them."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eval.lib.bands_io import read_rf_readings, write_rf_readings

SEG = {"rf_mid": 0.04, "rf_now": 0.0512, "rf_cons": 0.0475,
       "delta_cons": 0.0075, "rf_step": 0.005, "rf_cap": 0.005}
BASE = (249.62, 306.60, 361.61)
CONS = (215.70, 264.90, 312.63)
PRES = (225.87, 306.60, 404.67)


class RfReadingsIoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        write_rf_readings("GOOG.US", SEG, BASE, CONS, PRES, self.d)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trip(self) -> None:
        rd = read_rf_readings("GOOG.US", SEG, BASE, self.d)
        self.assertFalse(rd["stale"])
        self.assertEqual(rd["consensus"], CONS)
        self.assertEqual(rd["pressure"], PRES)
        self.assertAlmostEqual(rd["delta_cons"], 0.0075)

    def test_missing_is_none(self) -> None:
        self.assertIsNone(read_rf_readings("MSFT.US", SEG, BASE, self.d))

    def test_axis_move_marks_stale(self) -> None:
        moved = dict(SEG, rf_cons=0.05, delta_cons=0.01)
        self.assertTrue(read_rf_readings("GOOG.US", moved, BASE, self.d)["stale"])

    def test_rf_now_alone_does_not_stale(self) -> None:
        # the current 10Y feeds no column, so a daily 10Y print must not stale it
        self.assertFalse(read_rf_readings("GOOG.US", dict(SEG, rf_now=0.049), BASE, self.d)["stale"])

    def test_display_rounding_is_not_stale(self) -> None:
        # frameworks print anchors at 1dp (14.7) while the eval stores 14.74
        self.assertFalse(read_rf_readings("GOOG.US", SEG, (249.6, 306.6, 361.6), self.d)["stale"])

    def test_anchor_move_marks_stale(self) -> None:
        self.assertTrue(read_rf_readings("GOOG.US", SEG, (249.62, 310.0, 361.61), self.d)["stale"])


if __name__ == "__main__":
    unittest.main()
