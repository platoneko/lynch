"""Table of contents for a framework / axis doc: every heading with its line range and size, plus
one short line per §2 Assumptions row. Readers (subagents especially) look here first and then Read
only the lines they need, instead of paging through a 50–170KB document.

    python3 -m eval.lib.outline INTC.US          # frameworks/INTC.US.md
    python3 -m eval.lib.outline frameworks/axes/sic.md
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from eval.lib.frameworks import FRAMEWORKS_DIR

_HEADING = re.compile(r"^(#{1,6})\s")
_ASSUMPTION_ROW = re.compile(r"^\|\s*(A[^|\s]*)\s*\|")
# The status is the first cell that opens with the enum (`` ` `` / `**` and a trailing note allowed):
# claims may carry a bare `|ROE − r|`, so counting columns misplaces it.
_STATUS_CELL = re.compile(r"\|\s*[`*]*(active|confirmed|weakened|falsified)\b[^|]*\|")
_CLAIM_CHARS = 30


def _headings(lines: list[str]) -> list[tuple[int, int]]:
    """(line index, level) of each heading; `#` inside fenced code is a comment, not a heading."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and (m := _HEADING.match(line)):
            out.append((i, len(m.group(1))))
    return out


def _assumption_line(lineno: int, row: str) -> str | None:
    m = _ASSUMPTION_ROW.match(row)
    if not m:
        return None
    # | id | 假设 | 状态 | 证据 | 影响 |
    status = s.group(1) if (s := _STATUS_CELL.search(row)) else "?"
    claim = row[m.end():s.start() if s else None].strip()
    if len(claim) > _CLAIM_CHARS:
        claim = claim[:_CLAIM_CHARS] + "…"
    return f"    L{lineno} {m.group(1)} [{status}] {claim}"


def outline(md: str) -> str:
    lines = md.splitlines()
    heads = _headings(lines)
    out = []
    for k, (start, level) in enumerate(heads):
        # A heading's range runs until the next heading at the same or a higher level.
        end = next((i for i, lv in heads[k + 1:] if lv <= level), len(lines)) - 1
        size = sum(len(l) + 1 for l in lines[start:end + 1])
        out.append(f"L{start + 1}-{end + 1} ({size} chars) {lines[start]}")
        if level == 2 and lines[start].startswith("## 2."):
            sub_end = next((i for i, _ in heads[k + 1:]), len(lines))
            out += [a for i in range(start + 1, sub_end) if (a := _assumption_line(i + 1, lines[i]))]
    return "\n".join(out)


def _resolve(arg: str) -> Path:
    p = Path(arg)
    return p if p.suffix == ".md" else FRAMEWORKS_DIR / f"{arg}.md"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 -m eval.lib.outline <TICKER | path/to/doc.md>")
    print(outline(_resolve(sys.argv[1]).read_text(encoding="utf-8")))
