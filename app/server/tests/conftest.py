"""Fake frameworks/ and bands/ trees shaped like the real repo."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

AS_OF = "2026-09-26T09:03:00+08:00"


def framework_md(ticker: str, name: str, lynch: str, blocked: str | None = None) -> str:
    lines = [
        f"# {ticker} — {name}",
        "",
        f"> 🟡 林奇：{lynch} | 更新：2026-09-21 | eval: eval/{ticker}.py",
        "> 主尺：test ruler",
    ]
    if blocked:
        lines.append(f"> **状态：BLOCKED**（{blocked}）")
    lines += ["", "## 0. 现行结论", "", "- **价带锚点**（USD）：L = **10** · M = **20** · H = **40**", ""]
    return "\n".join(lines)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    fw = tmp_path / "frameworks"
    bands = tmp_path / "bands"
    fw.mkdir()
    bands.mkdir()
    (fw / "AAA.US.md").write_text(framework_md("AAA.US", "Alpha Corp", "`fast_grower` / `cyclical`"), encoding="utf-8")
    (fw / "BBB.US.md").write_text(
        framework_md("BBB.US", "Beta Inc", "`stalwart`", blocked="test reason; unblock when X"), encoding="utf-8"
    )
    (fw / "0001.HK.md").write_text("# 0001.HK — Broken\n\nno anchors here\n", encoding="utf-8")
    (fw / "_TEMPLATE.md").write_text("# template\n", encoding="utf-8")
    (bands / "AAA.US.json").write_text(
        json.dumps({"price": 25.0, "as_of": AS_OF, "zone": "neutral", "option_zone": None,
                    "candidate_zone": "optimistic", "anchors": {"L": 10}}),
        encoding="utf-8",
    )
    (bands / "BBB.US.json").write_text(
        json.dumps({"price": 5.0, "as_of": AS_OF, "zone": None, "option_zone": None, "candidate_zone": None}),
        encoding="utf-8",
    )
    return fw, bands
