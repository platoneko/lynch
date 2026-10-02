"""Axis header reader: mounts, kind, short label, and the 共识 line (AGENTS.md「共享情景轴」)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eval.lib.axes import Consensus, header_consensus, list_axis_paths, load_axis, parse_consensus
from eval.lib.frameworks import FRAMEWORKS_DIR

_NEEDS_CONTENT = unittest.skipUnless(FRAMEWORKS_DIR.is_dir(), "content repo not cloned at frameworks/")


def _write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / f"{name}.md"
    p.write_text(body, encoding="utf-8")
    return p


class ParseConsensusTests(unittest.TestCase):
    def test_tier_and_note(self) -> None:
        self.assertEqual(
            parse_consensus("> 共识：L（高盛 2027E 供应潮；as-of 2026-09-03）"),
            Consensus("L", "高盛 2027E 供应潮；as-of 2026-09-03"),
        )

    def test_between_tiers_accepts_ascii_hyphen(self) -> None:
        self.assertEqual(parse_consensus("> 共识：M-H（x）").tier, "M–H")
        self.assertEqual(parse_consensus("> 共识：M–H").tier, "M–H")

    def test_off_ladder(self) -> None:
        self.assertEqual(parse_consensus("> 共识：>H（x）").tier, ">H")
        self.assertEqual(parse_consensus("> 共识：<L（x）").tier, "<L")

    def test_bad_tier_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_consensus("> 共识：高（x）")
        with self.assertRaises(ValueError):
            parse_consensus("> 共识：L–H（x）")

    def test_other_lines_are_not_consensus(self) -> None:
        self.assertIsNone(parse_consensus("> 口径：共识中枢"))
        self.assertIsNone(parse_consensus("共识：L"))

    def test_header_only(self) -> None:
        md = "# X — y\n\n> 林奇：`cyclical`\n\n正文\n> 共识：H（not header）\n"
        self.assertIsNone(header_consensus(md))


class LoadAxisTests(unittest.TestCase):
    def test_mounts_stop_at_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d), "coal", (
                "# _AXIS.coal — 煤价情景轴（长协 regime）\n"
                "> 轴类型：`shared_scenario_axis` · **零和** | 更新：2026-09-27 | "
                "挂载票：2（`000543.SZ` · `600011.SH`；候选：`1898.HK`）\n"
                "> 共识：M–H（卖方 2027；as-of 2026-09-20）\n"
            ))
            a = load_axis(p)
        self.assertEqual(a.mounts, ("000543.SZ", "600011.SH"))
        self.assertEqual(a.kind, "shared_scenario_axis")
        self.assertEqual(a.label, "煤价")
        self.assertEqual(a.logical_id, "_AXIS.coal")
        self.assertEqual(a.consensus, Consensus("M–H", "卖方 2027；as-of 2026-09-20"))
        self.assertTrue(a.mounts_text.startswith("挂载票："))
        self.assertEqual(a.refs, ())

    def test_refs_after_the_reference_mark(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d), "crypto", (
                "# _AXIS.crypto — 加密周期情景轴\n"
                "> 轴类型：`shared_scenario_axis` | 更新：2026-09-23 | "
                "挂载票（全档）：COIN.US ｜ 参照（**仅 L 档**）：HOOD.US · 参照（构成方）：MSFT.US\n"
            ))
            a = load_axis(p)
        self.assertEqual(a.mounts, ("COIN.US",))
        self.assertEqual(a.refs, ("HOOD.US", "MSFT.US"))

    def test_refs_stop_at_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d), "coal", (
                "# _AXIS.coal — 煤价情景轴\n"
                "> 轴类型：`shared_scenario_axis` | 挂载票：— ｜ 参照：`000543.SZ` · `600011.SH`；候选：`1898.HK`\n"
            ))
            a = load_axis(p)
        self.assertEqual(a.mounts, ())
        self.assertEqual(a.refs, ("000543.SZ", "600011.SH"))

    def test_no_consensus_line(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d), "x", "# _AXIS.x — X 情景轴\n> 轴类型：`rates_axis` | 挂载票：全部 USD 段票\n")
            a = load_axis(p)
        self.assertIsNone(a.consensus)
        self.assertEqual(a.kind, "rates_axis")
        self.assertEqual(a.mounts, ())

    @_NEEDS_CONTENT
    def test_repo_axes_parse(self) -> None:
        axes = {a.name: a for a in map(load_axis, list_axis_paths())}
        self.assertEqual(axes["rates"].kind, "rates_axis")
        self.assertIn("601600.SH", axes["fed-path-aluminium"].mounts)
        self.assertEqual(axes["cowos-capacity"].refs, ("TSM.US",))
        self.assertNotIn("TSM.US", axes["cowos-capacity"].mounts)
        self.assertEqual(axes["fed-path-aluminium"].label, "电解铝价差")


if __name__ == "__main__":
    unittest.main()
