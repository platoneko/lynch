#!/usr/bin/env python3
"""Generate frameworks/_INDEX.md from framework + axis titles.

Derived view only — not a second source of truth (AGENTS.md: pool = scan
frameworks/*.md; axes = frameworks/axes/<name>.md, logical id `_AXIS.<name>`).
US rows are ticker links; HK/A rows add the Chinese name parsed from
`# TICKER — 中文 / English`. Axis rows link under ./axes/ and list mounts.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.lib.axes import list_axis_paths  # noqa: E402
from eval.lib.frameworks import FRAMEWORKS_DIR, list_framework_paths  # noqa: E402

_TITLE = re.compile(r"^#\s+(\S+)\s+[—–-]\s+(.+)$")
_INDEX_PATH = FRAMEWORKS_DIR / "_INDEX.md"

# (section heading, sort key for section order)
_SECTIONS: tuple[tuple[str, str], ...] = (
    ("美股", "US"),
    ("港股", "HK"),
    ("A股", "A"),
)


def _market_bucket(ticker: str) -> str | None:
    if ticker.endswith(".US"):
        return "US"
    if ticker.endswith(".HK"):
        return "HK"
    if ticker.endswith((".SH", ".SZ")):
        return "A"
    return None


def _chinese_name(name_rest: str) -> str | None:
    """Take the segment between em-dash and `/`; None if unparseable."""
    rest = name_rest.strip()
    if " / " in rest:
        cn = rest.split(" / ", 1)[0].strip()
    elif "/" in rest:
        cn = rest.split("/", 1)[0].strip()
    else:
        return None
    if not cn or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .,&'()-]*", cn):
        # Latin-only → treat as missing Chinese (e.g. US-style English title)
        return None
    return cn


def _parse_title(path: Path) -> tuple[str, str | None]:
    """Return (ticker, chinese_or_none). Ticker falls back to stem."""
    first = ""
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                first = line.rstrip("\n")
                break
    m = _TITLE.match(first)
    if not m:
        return path.stem, None
    ticker, rest = m.group(1), m.group(2)
    return ticker, _chinese_name(rest)


def _parse_axis(path: Path) -> tuple[str, str, str]:
    """Return (logical_id, title_rest, mounts_blob)."""
    logical = f"_AXIS.{path.stem}"
    title_rest = path.stem
    mounts = ""
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i == 0:
                m = _TITLE.match(line.rstrip("\n"))
                if m:
                    logical = m.group(1)
                    title_rest = m.group(2).strip()
                continue
            s = line.strip()
            if not s:
                if mounts:
                    break
                continue
            if s.startswith(">"):
                body = s.lstrip("> ").strip()
                if "挂载票" in body:
                    # Keep from first 挂载票 onwards; drop leading meta before it.
                    idx = body.find("挂载票")
                    mounts = body[idx:].strip()
                    # Later quote lines may continue mounts (rare); keep first hit.
                    break
            elif mounts or i > 8:
                break
    return logical, title_rest, mounts


def build_index(frameworks_dir: Path | None = None) -> str:
    buckets: dict[str, list[tuple[str, str | None]]] = {k: [] for _, k in _SECTIONS}
    unknown: list[str] = []

    for path in list_framework_paths(frameworks_dir):
        # Link by filename stem so the relative path always resolves.
        ticker = path.stem
        _, cn = _parse_title(path)
        bucket = _market_bucket(ticker)
        if bucket is None:
            unknown.append(ticker)
            continue
        buckets[bucket].append((ticker, cn))

    for items in buckets.values():
        items.sort(key=lambda t: t[0])

    lines: list[str] = [
        "# 标的与轴索引",
        "",
        "> 由 `scripts/gen_index.py` 从 `frameworks/*.md` 与 `frameworks/axes/*.md` 题头生成；勿手改。",
        "> 轴的稳定引用写逻辑 id `` `_AXIS.<名>` ``；文件在 `axes/<名>.md`。",
        "",
    ]
    for heading, key in _SECTIONS:
        items = buckets[key]
        if not items:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        for ticker, cn in items:
            link = f"[{ticker}](./{ticker}.md)"
            if key == "US":
                lines.append(f"- {link}")
            elif cn:
                lines.append(f"- {link} — {cn}")
            else:
                lines.append(f"- {link}")
        lines.append("")

    if unknown:
        lines.append("## 其他")
        lines.append("")
        for ticker in sorted(unknown):
            lines.append(f"- [{ticker}](./{ticker}.md)")
        lines.append("")

    axes = list_axis_paths()
    lines.append("## 共享情景轴")
    lines.append("")
    if not axes:
        lines.append("_（尚无 `frameworks/axes/*.md`）_")
        lines.append("")
    else:
        for path in axes:
            logical, title_rest, mounts = _parse_axis(path)
            link = f"[`{logical}`](./axes/{path.name})"
            if mounts:
                lines.append(f"- {link} — {title_rest} · {mounts}")
            else:
                lines.append(f"- {link} — {title_rest}")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    text = build_index()
    _INDEX_PATH.write_text(text, encoding="utf-8")
    n_tickers = sum(
        1
        for line in text.splitlines()
        if line.startswith("- [") and "./axes/" not in line
    )
    n_axes = sum(1 for line in text.splitlines() if "./axes/" in line)
    print(
        f"wrote {_INDEX_PATH.relative_to(REPO_ROOT)} "
        f"({n_tickers} tickers, {n_axes} axes)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
