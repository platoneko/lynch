"""Lightweight tests for zone mapping and framework anchor parse."""
from __future__ import annotations

import unittest
from pathlib import Path

from eval.lib.frameworks import load_all_frameworks, load_framework
from eval.lib.zones import cut_points, zone_bound_edges, zone_of


class ZoneTests(unittest.TestCase):
    def test_midpoints(self) -> None:
        A, B = cut_points(10, 20, 40)
        self.assertEqual(A, 15)
        self.assertEqual(B, 30)

    def test_zone_boundaries(self) -> None:
        L, M, H = 10.0, 20.0, 40.0
        self.assertEqual(zone_of(9.99, L, M, H), "deep_discount")
        self.assertEqual(zone_of(10.0, L, M, H), "conservative")
        self.assertEqual(zone_of(14.99, L, M, H), "conservative")
        self.assertEqual(zone_of(15.0, L, M, H), "neutral")
        self.assertEqual(zone_of(29.99, L, M, H), "neutral")
        self.assertEqual(zone_of(30.0, L, M, H), "optimistic")
        self.assertEqual(zone_of(40.0, L, M, H), "optimistic")
        self.assertEqual(zone_of(40.01, L, M, H), "stretched")

    def test_zone_bound_edges(self) -> None:
        L, M, H = 10.0, 20.0, 40.0
        A, B = cut_points(L, M, H)
        self.assertEqual(zone_bound_edges("deep_discount", L, M, H), ("open_below", None, L))
        self.assertEqual(zone_bound_edges("conservative", L, M, H), ("half_open", L, A))
        self.assertEqual(zone_bound_edges("neutral", L, M, H), ("half_open", A, B))
        self.assertEqual(zone_bound_edges("optimistic", L, M, H), ("closed", B, H))
        self.assertEqual(zone_bound_edges("stretched", L, M, H), ("open_above", H, None))


class ParseTests(unittest.TestCase):
    def test_all_frameworks_parse(self) -> None:
        metas = load_all_frameworks()
        tickers = {m.ticker for m in metas}
        self.assertIn("INTC.US", tickers)
        self.assertIn("WOLF.US", tickers)
        self.assertIn("0700.HK", tickers)
        intc = next(m for m in metas if m.ticker == "INTC.US")
        self.assertAlmostEqual(intc.L, 27.2, places=1)
        self.assertAlmostEqual(intc.M, 71.2, places=1)
        self.assertAlmostEqual(intc.H, 113.1, places=1)

    def test_stem_matches_title(self) -> None:
        root = Path(__file__).resolve().parents[2] / "frameworks"
        for p in root.glob("*.md"):
            if p.name.startswith("_"):
                continue
            m = load_framework(p)
            self.assertEqual(m.ticker, p.stem)


if __name__ == "__main__":
    unittest.main()
