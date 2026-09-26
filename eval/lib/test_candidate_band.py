"""Consensus-upgrade candidate band: parse → digest line → cross alert → artifact.

The candidate band is the base deck's *next version* previewed before a
`待补` input (usually a missing ≥2-house consensus year) arrives.  It is not
an option: no p_k, no probability, and it never feeds the base zone.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import daily_bands  # noqa: E402
from eval.lib.bands_io import build_record  # noqa: E402
from eval.lib.frameworks import FrameworkMeta, load_framework  # noqa: E402

_BASE_MD = """# TEST.US — 测试 / Test Inc.

> 林奇：`fast_grower` / `cyclical` | 更新：2026-09-12 | eval: eval/TEST.US.py
> 主尺：x

## 0. 现行结论

- **价带锚点（USD/股）**：L = **7.23** · M = **13.02** · H = **13.88**

## 3. 可演算块

### Outputs — 锚点与五档区间

- 底牌 L = **7.23** · M = **13.02** · H = **13.88** · 切点 A = **10.13** · B = **13.45**
- \\(V_{{opt}}\\) = **0**
{candidate}
- 现价 17.32 → zone = **`stretched`**

## 4. Changelog
"""

_CANDIDATE_LINE = "- 候选带（FY28·Piper单家）：L′ = **7.23** · M′ = **15.54** · H′ = **16.29** · 门槛 A22"


def _load(candidate: str) -> FrameworkMeta:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "TEST.US.md"
        p.write_text(_BASE_MD.format(candidate=candidate), encoding="utf-8")
        return load_framework(p)


def _meta(**kwargs) -> FrameworkMeta:
    base = dict(
        ticker="SOFI.US",
        path=Path("frameworks/SOFI.US.md"),
        name="SoFi 科技 / SoFi Technologies, Inc.",
        L=7.23,
        M=13.02,
        H=13.88,
        currency="USD",
        eval_script=None,
        lynch_primary="fast_grower",
        lynch_secondary="cyclical",
    )
    base.update(kwargs)
    return FrameworkMeta(**base)


def _row(meta: FrameworkMeta, price: float, zone: str, **kwargs) -> daily_bands.Row:
    return daily_bands.Row(
        meta=meta,
        price=price,
        zone=zone,
        previous_zone=kwargs.get("previous_zone"),
        crossed=kwargs.get("crossed", False),
        notes=kwargs.get("notes", []),
        record={},
        option_zone=kwargs.get("option_zone"),
        previous_option_zone=kwargs.get("previous_option_zone"),
        option_crossed=kwargs.get("option_crossed", False),
        candidate_zone=kwargs.get("candidate_zone"),
        previous_candidate_zone=kwargs.get("previous_candidate_zone"),
        candidate_crossed=kwargs.get("candidate_crossed", False),
    )


class ParseCandidateTests(unittest.TestCase):
    def test_candidate_line_parsed_with_label(self) -> None:
        m = _load(_CANDIDATE_LINE)
        self.assertEqual(m.candidate, (7.23, 15.54, 16.29))
        self.assertEqual(m.candidate_label, "FY28·Piper单家")

    def test_candidate_does_not_disturb_base_anchors(self) -> None:
        m = _load(_CANDIDATE_LINE)
        self.assertEqual((m.L, m.M, m.H), (7.23, 13.02, 13.88))

    def test_no_candidate_is_none(self) -> None:
        m = _load("")
        self.assertIsNone(m.candidate)
        self.assertIsNone(m.candidate_label)
        self.assertIsNone(m.candidate_gate)

    def test_gate_is_read_after_the_primed_anchors(self) -> None:
        self.assertEqual(_load(_CANDIDATE_LINE).candidate_gate, "A22")
        line = "- 候选带（T）：L′ = **7.23** · M′ = **15.54** · H′ = **16.29** · 门槛 A3′（note 门槛 A1）"
        self.assertEqual(_load(line).candidate_gate, "A3′")

    def test_gate_named_only_in_the_label_is_none(self) -> None:
        # 600096.SH shape: the label mentions an id, but no `门槛 A…` follows the prices.
        line = "- 候选带（FY2027·单家，A3′）：L′ = **7.23** · M′ = **15.54** · H′ = **16.29**；门槛 FY2027 ≥2 家"
        m = _load(line)
        self.assertEqual(m.candidate_label, "FY2027·单家，A3′")
        self.assertIsNone(m.candidate_gate)

    def test_real_framework_gates(self) -> None:
        gates = {t: load_framework(REPO_ROOT / "frameworks" / f"{t}.md").candidate_gate
                 for t in ("0700.HK", "600096.SH", "0189.HK")}
        self.assertEqual(gates, {"0700.HK": "A1c", "600096.SH": None, "0189.HK": None})

    def test_candidate_must_be_a_ladder(self) -> None:
        bad = "- 候选带（x）：L′ = **7.23** · M′ = **16.29** · H′ = **15.54**"
        with self.assertRaises(ValueError):
            _load(bad)

    def test_two_candidate_lines_are_ambiguous(self) -> None:
        with self.assertRaises(ValueError):
            _load(_CANDIDATE_LINE + "\n" + _CANDIDATE_LINE)

    def test_sofi_framework_carries_candidate(self) -> None:
        m = load_framework(REPO_ROOT / "frameworks" / "SOFI.US.md")
        self.assertEqual(m.candidate, (5.56, 9.69, 10.15))
        self.assertEqual(m.candidate_label, "FY28·Piper单家·窗口口径")


class CandidateZoneTests(unittest.TestCase):
    def test_no_candidate_is_none(self) -> None:
        self.assertIsNone(daily_bands.candidate_zone(17.32, _meta()))

    def test_price_is_zoned_on_candidate_band(self) -> None:
        meta = _meta(candidate=(7.23, 15.54, 16.29), candidate_label="FY28·Piper单家")
        self.assertEqual(daily_bands.candidate_zone(17.32, meta), "stretched")
        self.assertEqual(daily_bands.candidate_zone(15.0, meta), "neutral")


class CandidateLinesTests(unittest.TestCase):
    def test_candidate_line_follows_base_line(self) -> None:
        meta = _meta(candidate=(7.23, 15.54, 16.29), candidate_label="FY28·Piper单家")
        r = _row(meta, price=17.32, zone="stretched", candidate_zone="stretched")
        self.assertEqual(
            daily_bands.format_pool_lines(r),
            [
                "🔴 SOFI.US 17.32 USD",
                "「叙事溢价」 P > 13.88 · L/M/H = 7.23/13.02/13.88",
                "「叙事溢价」 候选(FY28·Piper单家) P > 16.29 · L/M/H = 7.23/15.54/16.29",
            ],
        )

    def test_candidate_line_comes_after_plus_v_line(self) -> None:
        meta = _meta(
            v_opt=2.0,
            p_tiers=(0.0, 0.5, 1.0),
            candidate=(7.23, 15.54, 16.29),
            candidate_label="FY28·Piper单家",
        )
        r = _row(
            meta, price=17.32, zone="stretched",
            option_zone="stretched", candidate_zone="stretched",
        )
        lines = daily_bands.format_pool_lines(r)
        self.assertEqual(len(lines), 4)
        self.assertIn("+V(2)", lines[2])
        self.assertIn("候选(FY28·Piper单家)", lines[3])

    def test_no_candidate_no_line(self) -> None:
        r = _row(_meta(), price=17.32, zone="stretched")
        self.assertEqual(len(daily_bands.format_pool_lines(r)), 2)


class CandidateCrossTests(unittest.TestCase):
    def test_candidate_cross_counts_as_any_crossed(self) -> None:
        r = _row(_meta(), price=17.32, zone="stretched", candidate_crossed=True)
        self.assertTrue(r.any_crossed)

    def test_candidate_only_cross_emits_transition_line(self) -> None:
        meta = _meta(candidate=(7.23, 15.54, 16.29), candidate_label="FY28·Piper单家")
        r = _row(
            meta, price=16.0, zone="stretched", previous_zone="stretched",
            candidate_zone="optimistic", previous_candidate_zone="stretched",
            candidate_crossed=True,
        )
        _, lines = daily_bands.build_cross_post([r], as_of_local="x")
        self.assertEqual(
            lines,
            [
                "🔴 SOFI.US 16.00 USD",
                "「叙事溢价」 P > 13.88 · L/M/H = 7.23/13.02/13.88",
                "「乐观」 候选(FY28·Piper单家) [15.91, 16.29] · L/M/H = 7.23/15.54/16.29",
                "候选 「叙事溢价」 → 「乐观」",
            ],
        )


class CandidateRecordTests(unittest.TestCase):
    def test_record_persists_candidate_zones(self) -> None:
        rec = build_record(
            ticker="SOFI.US", as_of="x", price=17.32, currency="USD",
            L=7.23, M=13.02, H=13.88, zone="stretched", previous_zone=None,
            candidate_zone="stretched", previous_candidate_zone="optimistic",
        )
        self.assertEqual(rec["candidate_zone"], "stretched")
        self.assertEqual(rec["previous_candidate_zone"], "optimistic")

    def test_record_defaults_candidate_to_none(self) -> None:
        rec = build_record(
            ticker="X.US", as_of="x", price=1.0, currency="USD",
            L=1.0, M=2.0, H=3.0, zone="deep_discount", previous_zone=None,
        )
        self.assertIsNone(rec["candidate_zone"])
        self.assertIsNone(rec["previous_candidate_zone"])


if __name__ == "__main__":
    unittest.main()
