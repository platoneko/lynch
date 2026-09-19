"""Lightweight tests for zone mapping and framework anchor parse."""
from __future__ import annotations

import unittest
from pathlib import Path

from eval.lib.frameworks import FrameworkMeta, basket_label, list_framework_paths, load_all_frameworks, load_framework
from eval.lib.inputs import parse_baskets
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

    def test_paths_grouped_by_market_like_index(self) -> None:
        # Daily digest must read US → HK → A like _INDEX.md, not name-sorted
        # (which interleaves 2631.HK between 300014.SZ and 1898.HK).
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ("300014.SZ", "2631.HK", "0700.HK", "AEHR.US", "000100.SZ", "_INDEX"):
                (root / f"{name}.md").write_text("", encoding="utf-8")
            got = [p.stem for p in list_framework_paths(root)]
        self.assertEqual(got, ["AEHR.US", "0700.HK", "2631.HK", "000100.SZ", "300014.SZ"])

    def test_parse_baskets_reads_leg_baskets(self) -> None:
        md = (
            "### Inputs\n\n| 字段名 | 含义 | 取值 / 来源 | as-of |\n|---|---|---|---|\n"
            "| r_sw | 0.10 | 篮 4 龙头制造：软件年金 | 2026-09-20 |\n"
            "| r_oci | 0.15 | 篮 7 单押：融资未落定 | 2026-09-20 |\n"
            "| r_disc | 0.10 | 年漂移折现，不归篮 | 2026-09-20 |\n"
            "| g2 | 0.06 | 篮 4 下截顶 | 2026-09-20 |\n\n### Formula\n"
        )
        self.assertEqual(parse_baskets(md), {"r_sw": 4, "r_oci": 7})

    def test_blocked_header_is_parsed(self) -> None:
        import tempfile

        md = (
            "# X.US — 甲 / X Inc.\n\n"
            "> 林奇：`stalwart` | 更新：2026-09-20 | eval: eval/X.US.py\n"
            "> **状态：BLOCKED**（③ 出带：篮 4 仍出带；解封 = ROIC 证据 ≥14%）\n"
            "> 主尺：x\n\n## 3\n\n### Inputs\n\n| 字段名 | 含义 | 取值 | as-of |\n|---|---|---|---|\n"
            "| r_perp | 0.10 | 篮 4 龙头制造 | 2026-09-20 |\n\n"
            "### Outputs\n\n- 价带锚点（USD）对照带：L = **57.35** · M = **77.23** · H = **136.83**\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "X.US.md"
            p.write_text(md, encoding="utf-8")
            m = load_framework(p)
        self.assertEqual(m.blocked, "③ 出带：篮 4 仍出带；解封 = ROIC 证据 ≥14%")
        self.assertEqual(m.baskets, {"r_perp": 4})
        self.assertEqual((m.L, m.M, m.H), (57.35, 77.23, 136.83))

    def test_basket_label(self) -> None:
        base = dict(path=Path("x"), name="x", L=1, M=2, H=3, currency="USD", eval_script=None,
                    lynch_primary="stalwart", lynch_secondary=None)
        self.assertEqual(basket_label(FrameworkMeta(ticker="A", baskets={"r_perp": 4}, **base)), "篮 4 10%")
        self.assertEqual(basket_label(FrameworkMeta(ticker="B", baskets={"r_sw": 4, "r_oci": 7}, **base)),
                         "r_sw 篮 4 10% · r_oci 篮 7 15%")
        self.assertEqual(basket_label(FrameworkMeta(ticker="C", **base)), "—")

    def test_stem_matches_title(self) -> None:
        root = Path(__file__).resolve().parents[2] / "frameworks"
        for p in root.glob("*.md"):
            if p.name.startswith("_"):
                continue
            m = load_framework(p)
            self.assertEqual(m.ticker, p.stem)


if __name__ == "__main__":
    unittest.main()
