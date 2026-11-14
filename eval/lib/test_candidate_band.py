"""Consensus-upgrade candidate band: framework parsing.

The candidate band is the base deck's *next version* previewed before a
`待补` input (usually a missing ≥2-house consensus year) arrives.  It is not
an option: no p_k, no probability, and it never feeds the base zone.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
from eval.lib.frameworks import FRAMEWORKS_DIR, FrameworkMeta, list_framework_paths, load_framework  # noqa: E402

_NEEDS_CONTENT = unittest.skipUnless(FRAMEWORKS_DIR.is_dir(), "content repo not cloned at frameworks/")

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

    def test_candidate_must_be_a_ladder(self) -> None:
        bad = "- 候选带（x）：L′ = **7.23** · M′ = **16.29** · H′ = **15.54**"
        with self.assertRaises(ValueError):
            _load(bad)

    def test_two_candidate_lines_are_ambiguous(self) -> None:
        with self.assertRaises(ValueError):
            _load(_CANDIDATE_LINE + "\n" + _CANDIDATE_LINE)

    @_NEEDS_CONTENT
    def test_real_candidate_lines_parse(self) -> None:
        # Which tickers carry a band, and its numbers, are research content; only check every line is read.
        line = re.compile(r"^-\s+(?:\*\*)?候选带（", re.MULTILINE)
        for p in list_framework_paths():
            if line.search(p.read_text(encoding="utf-8")):
                self.assertIsNotNone(load_framework(p).candidate, p.name)
