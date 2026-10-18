"""outline: a framework doc's table of contents with line numbers, so readers fetch only the lines they need."""
from __future__ import annotations

import unittest

from eval.lib.frameworks import FRAMEWORKS_DIR
from eval.lib.outline import outline

_NEEDS_CONTENT = unittest.skipUnless(FRAMEWORKS_DIR.is_dir(), "content repo not cloned at frameworks/")

_DOC = """# X.US — 例

> 林奇：`stalwart`

## 0. 现行结论

正文

## 2. Assumptions

| id | 假设 | 状态 | 证据 | 影响 |
|----|------|------|------|------|
| A1 | 收入持续增长且这一句很长很长很长很长很长很长很长很长很长很长很长 | active | 长证据 | M |
| A2 | 利润率稳定 | `weakened` | 证据 | L |
| A3 | \\(\\|ROE-r\\|\\) 与 |g − r| 小 | active | 证据 | M |
| A4 | 份额 | **falsified**（2026-09-10） | 证据 | H |

### 2b. 子节

## 3. 可演算块
"""


class OutlineTests(unittest.TestCase):
    def test_sections_carry_line_ranges(self) -> None:
        text = outline(_DOC)
        self.assertIn("L5-8 ", text)    # §0 runs to the line before §2
        self.assertIn("## 0. 现行结论", text)
        self.assertIn("L18-19 ", text)  # nested heading keeps its own range
        self.assertIn("L20-20 ", text)  # last heading runs to end of file

    def test_assumption_rows_list_id_status_and_short_claim(self) -> None:
        text = outline(_DOC)
        self.assertIn("L13 A1 [active]", text)
        self.assertIn("L14 A2 [weakened] 利润率稳定", text)
        self.assertIn("L15 A3 [active]", text)  # a pipe inside the claim does not shift the status column
        self.assertIn("L16 A4 [falsified] 份额", text)  # bold status with a dated note
        self.assertNotIn("长证据", text)
        a1 = next(l for l in text.splitlines() if " A1 " in l)
        self.assertLess(len(a1), 70)

    @_NEEDS_CONTENT
    def test_real_framework_outline_is_small(self) -> None:
        path = FRAMEWORKS_DIR / "INTC.US.md"
        if not path.exists():
            self.skipTest("INTC.US.md absent")
        self.assertLess(len(outline(path.read_text(encoding="utf-8"))), 4000)


if __name__ == "__main__":
    unittest.main()
