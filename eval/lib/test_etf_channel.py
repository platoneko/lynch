"""eval.lib.etf channel maths (valuation-rules.md「§ETF 例外」)."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from eval.lib.etf import channel, slope_since


def _synthetic(slope: float = 0.08, n_years: int = 6) -> pd.Series:
    idx = pd.date_range("2019-01-01", periods=n_years * 252, freq="B")
    t = np.arange(len(idx)) / 252.0
    rng = np.random.default_rng(0)
    y = np.exp(np.log(1000.0) + slope * t + rng.normal(0, 0.02, len(idx)))
    return pd.Series(y, index=idx)


class ChannelTests(unittest.TestCase):
    def test_slope_recovers_trend(self) -> None:
        c = channel(_synthetic(0.08))
        self.assertAlmostEqual(c["slope"], np.exp(0.08) - 1, delta=0.01)
        self.assertLess(c["L"], c["M"])
        self.assertLess(c["M"], c["H"])

    def test_slope_since_subwindow(self) -> None:
        s = _synthetic(0.05)
        self.assertAlmostEqual(slope_since(s, "2021-01-01"), np.exp(0.05) - 1, delta=0.015)


if __name__ == "__main__":
    unittest.main()
