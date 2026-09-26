"""_AXIS.rates: segment loader (valuation-rules「利率轴」)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eval.lib.axes import load_rates
from eval.lib.frameworks import FRAMEWORKS_DIR

_NEEDS_CONTENT = unittest.skipUnless(FRAMEWORKS_DIR.is_dir(), "content repo not cloned at frameworks/")

_USD_ROW = "| **USD** | **4.00%** | **5.12%** | **4.75%** | **+75bp** | **50bp** | **50bp** | 2026-09-25 |"


def _axis(tmp: Path, usd_row: str) -> Path:
    p = tmp / "rates.md"
    p.write_text(
        "| 段 | 中枢 | 当前 | 共识 | Δ | step | cap | as-of |\n"
        "|----|---|---|---|---|---|---|---|\n"
        f"{usd_row}\n"
        "| CNY | 2.50% | `待建` | `待建` | `待建` | — | — | x |\n",
        encoding="utf-8",
    )
    return p


class LoadRatesTests(unittest.TestCase):
    @_NEEDS_CONTENT
    def test_repo_axis_usd(self) -> None:
        seg = load_rates("USD")
        self.assertAlmostEqual(seg["rf_mid"], 0.04)
        self.assertAlmostEqual(seg["delta_cons"], seg["rf_cons"] - seg["rf_mid"])

    @_NEEDS_CONTENT
    def test_hkd_follows_usd(self) -> None:
        self.assertEqual(load_rates("HKD"), load_rates("USD"))

    def test_parses_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            seg = load_rates("USD", _axis(Path(d), _USD_ROW))
        self.assertEqual(seg, {"rf_mid": 0.04, "rf_now": 0.0512, "rf_cons": 0.0475,
                               "delta_cons": 0.0075, "rf_step": 0.005, "rf_cap": 0.005})

    def test_stated_delta_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = _axis(Path(d), _USD_ROW.replace("+75bp", "+50bp"))
            with self.assertRaisesRegex(ValueError, "Δ_cons"):
                load_rates("USD", p)

    def test_unbuilt_segment_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = _axis(Path(d), _USD_ROW)
            with self.assertRaisesRegex(ValueError, "CNY"):
                load_rates("CNY", p)


if __name__ == "__main__":
    unittest.main()
