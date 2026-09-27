"""Lightweight tests for zone mapping and framework anchor parse."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eval.lib.frameworks import (
    FRAMEWORKS_DIR, FrameworkMeta, basket_label, list_framework_paths, load_all_frameworks, load_framework,
)
from eval.lib.inputs import parse_baskets
from eval.lib.zones import credited_option_band, cut_points, price_zones, zone_bound_edges, zone_of

_NEEDS_CONTENT = unittest.skipUnless(FRAMEWORKS_DIR.is_dir(), "content repo not cloned at frameworks/")


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

    def test_price_zones_base_option_candidate(self) -> None:
        base = dict(ticker="X.US", path=Path("x"), name="x", L=10.0, M=20.0, H=40.0, currency="USD",
                    eval_script=None, lynch_primary="stalwart", lynch_secondary=None)
        plain = FrameworkMeta(**base)
        # +V band = (10, 25, 50); candidate (36, 50, 60) sits above the price.
        full = FrameworkMeta(**base, v_opt=10.0, p_tiers=(0.0, 0.5, 1.0),
                             candidate=(36.0, 50.0, 60.0), candidate_label="T")
        self.assertEqual(price_zones(35.0, plain), ("optimistic", None, None))
        self.assertEqual(price_zones(35.0, full), ("optimistic", "neutral", "deep_discount"))


class ParseTests(unittest.TestCase):
    @_NEEDS_CONTENT
    def test_all_frameworks_parse(self) -> None:
        metas = load_all_frameworks()
        tickers = {m.ticker for m in metas}
        self.assertIn("INTC.US", tickers)
        self.assertIn("WOLF.US", tickers)
        self.assertIn("0700.HK", tickers)
        intc = next(m for m in metas if m.ticker == "INTC.US")
        self.assertAlmostEqual(intc.L, 14.7, places=1)
        self.assertAlmostEqual(intc.M, 29.2, places=1)
        self.assertAlmostEqual(intc.H, 33.6, places=1)

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

    def test_parse_baskets_ignores_non_r_rows_that_mention_a_basket(self) -> None:
        md = (
            "### Inputs\n\n| 字段名 | 含义 | 取值 / 来源 | as-of |\n|---|---|---|---|\n"
            "| r | 0.10 | 篮 4 龙头制造 | 2026-09-20 |\n"
            "| roe_m | 0.18 | 篮 4 同业中位；不是 r 行 | 2026-09-20 |\n"
            "| rf_ccy_mid | 0.03 | 篮 5 对照用无风险利率 | 2026-09-20 |\n\n### Formula\n"
        )
        self.assertEqual(parse_baskets(md), {"r": 4})

    @staticmethod
    def _load_md(md: str) -> FrameworkMeta:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "X.US.md"
            p.write_text(md, encoding="utf-8")
            return load_framework(p)

    def test_basket_label(self) -> None:
        base = dict(path=Path("x"), name="x", L=1, M=2, H=3, currency="USD", eval_script=None,
                    lynch_primary="stalwart", lynch_secondary=None)
        self.assertEqual(basket_label(FrameworkMeta(ticker="A", baskets={"r_perp": 4}, **base)), "篮 4 10%")
        self.assertEqual(basket_label(FrameworkMeta(ticker="B", baskets={"r_sw": 4, "r_oci": 7}, **base)),
                         "r_sw 篮 4 10% · r_oci 篮 7 15%")
        # Same basket on every leg (A/H companion rows) collapses to one label.
        self.assertEqual(basket_label(FrameworkMeta(ticker="D", baskets={"r_perp": 4, "r_perp_a": 4}, **base)),
                         "篮 4 10%")
        self.assertEqual(basket_label(FrameworkMeta(ticker="C", **base)), "—")

    @_NEEDS_CONTENT
    def test_stem_matches_title(self) -> None:
        for p in list_framework_paths():
            m = load_framework(p)
            self.assertEqual(m.ticker, p.stem)


class OptionTests(unittest.TestCase):
    """The +V option band and its Outputs line (AGENTS.md「未建模期权」)."""

    _MD = (
        "# X.US — 甲 / X Inc.\n\n"
        "> 林奇：`stalwart` | 更新：2026-09-20 | eval: eval/X.US.py\n"
        "> 主尺：x\n\n## 0. 现行结论\n\n"
        "- **价带锚点（USD）**：L = **10** · M = **20** · H = **40**\n"
        "  - \\(V_{opt}\\) = **10**（§0 shorthand, not the Outputs line）\n\n"
        "## 3. 可演算块\n\n### Outputs\n\n"
        "- 底牌 L = **10** · M = **20** · H = **40**\n"
        "{outputs}"
        "- **可比性读数**：\\(L/M\\) = **0.5** · \\(V_{opt}/M\\) = **0.5**\n\n"
        "## 4. Changelog\n\n- \\(V_{opt}\\) = **99** later note\n"
    )

    def _load(self, outputs: str) -> FrameworkMeta:
        return ParseTests._load_md(self._MD.replace("{outputs}", outputs))

    def test_option_line_is_the_outputs_bullet(self) -> None:
        # 000100.SZ shape: §0 names V_opt first; the Outputs bullet is the one reported.
        m = self._load("- \\(V_{opt}\\) = **10** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0** —— 见表3（A12）\n")
        self.assertEqual(m.option_line, "\\(V_{opt}\\) = **10** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0** —— 见表3（A12）")

    def test_option_values_come_from_outputs_not_earlier_prose(self) -> None:
        # ORCL.US / WOLF.US shape: a §0 sentence named another V_opt before Outputs.
        md = self._MD.replace("{outputs}", "- \\(V_{opt}\\) = **12** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0**\n")
        m = ParseTests._load_md(md.replace("（§0 shorthand", " · \\(p_L/p_M/p_H\\) = **0 / 0.2 / 0.3**（§0 shorthand"))
        self.assertEqual((m.v_opt, m.p_tiers), (12.0, (0.0, 0.5, 1.0)))

    def test_no_outputs_bullet_is_none(self) -> None:
        self.assertIsNone(self._load("").option_line)

    def test_credited_option_band(self) -> None:
        with_p = self._load("- \\(V_{opt}\\) = **10** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0**\n")
        self.assertEqual(credited_option_band(with_p), (10.0, 25.0, 50.0))
        # A ceiling without the per-tier credit places no band.
        self.assertIsNone(credited_option_band(self._load("- \\(V_{opt}\\) = **10**\n")))
        zero = ParseTests._load_md(
            self._MD.replace("{outputs}", "- \\(V_{opt}\\) = **0** · \\(p_L/p_M/p_H\\) = **0 / 0.5 / 1.0**\n")
            .replace("**10**（§0", "**0**（§0"))
        self.assertEqual(zero.v_opt, 0.0)
        self.assertIsNone(credited_option_band(zero))

    @_NEEDS_CONTENT
    def test_real_frameworks_option_lines(self) -> None:
        with_option = [m for m in load_all_frameworks() if credited_option_band(m) is not None]
        self.assertTrue(with_option)
        for m in with_option:
            with self.subTest(ticker=m.ticker):
                self.assertIsNotNone(m.option_line)
                self.assertTrue(m.option_line.startswith("\\(V_{opt}\\)"), m.option_line)
        cohr = next(m for m in with_option if m.ticker == "COHR.US")
        self.assertIn("（A12）", cohr.option_line)


REPO_ROOT = Path(__file__).resolve().parents[2]


class ContentRepoLayoutTest(unittest.TestCase):
    """Content lives in lynch-frameworks, cloned at lynch/frameworks/."""

    def test_missing_frameworks_dir_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(FileNotFoundError, "lynch-frameworks"):
                list_framework_paths(Path(d) / "frameworks")

    def test_empty_frameworks_dir_raises(self) -> None:
        # A present-but-empty checkout (e.g. an aborted clone) must not read as an empty pool either.
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(FileNotFoundError, "lynch-frameworks"):
                list_framework_paths(Path(d))

    def test_repo_docs_are_not_tickers(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ("AGENTS.md", "README.md", "_TEMPLATE.md", "INTC.US.md"):
                (root / name).write_text("x")
            self.assertEqual([p.name for p in list_framework_paths(root)], ["INTC.US.md"])

    @_NEEDS_CONTENT
    def test_eval_pointer_resolves_under_content_repo(self) -> None:
        meta = load_framework(FRAMEWORKS_DIR / "GOOG.US.md")
        self.assertEqual(meta.eval_script, FRAMEWORKS_DIR / "eval" / "GOOG.US.py")

    def test_no_ticker_scripts_in_code_repo(self) -> None:
        # Per-ticker evals belong in frameworks/eval/; eval/ here holds only the library.
        self.assertEqual(sorted(p.name for p in (REPO_ROOT / "eval").glob("*.py")), [])


if __name__ == "__main__":
    unittest.main()
