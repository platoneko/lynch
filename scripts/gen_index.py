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
SERVER_ROOT = REPO_ROOT / 'app' / 'server'
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from eval.lib.frameworks import (FRAMEWORKS_DIR, list_framework_paths, market_bucket,
                                 parse_framework_identity)  # noqa: E402
from eval.lib.result_view import SOURCE_STATUS_MESSAGES, map_result, status_text  # noqa: E402
from eval.lib.result_service import open_result_reader  # noqa: E402
from eval.lib.axis_projection import collect_axes as collect_axis_projection, project_axes  # noqa: E402
from eval.lib.axes import list_axis_paths  # noqa: E402
from lynch_reader.archive import ARCHIVE_FILE, FrameworkArchive  # noqa: E402

_TITLE = re.compile(r"^#\s+(\S+)\s+[—–-]\s+(.+)$")
_INDEX_PATH = FRAMEWORKS_DIR / "_INDEX.md"

# (section heading, sort key for section order)
_SECTIONS: tuple[tuple[str, str], ...] = (
    ("美股", "US"),
    ("港股", "HK"),
    ("A股", "A"),
)


def _chinese_name(name_rest: str) -> str | None:
    """Take the segment between em-dash and `/`; None if unparseable."""
    rest = name_rest.strip()
    if " / " in rest:
        cn = rest.split(" / ", 1)[0].strip()
    elif "/" in rest:
        cn = rest.split("/", 1)[0].strip()
    else:
        cn = rest
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


def build_index_from_results(frameworks_dir: Path, provider, *, archived, axis_metadata=None, axis_titles=None) -> str:
    """Draft index using human headers and injected machine results only.

    The normal entry still uses the old generator until the joint switch. The
    caller supplies axis metadata; this path never parses Markdown prices or
    the former Inputs table for a basket.
    """
    buckets = {key: [] for _, key in _SECTIONS}
    unknown = []
    collect_axes = axis_metadata is None
    projections = {}
    axis_unknown = False
    if collect_axes:
        axis_metadata = []
    for path in list_framework_paths(frameworks_dir):
        ticker = path.stem
        identity = parse_framework_identity(path.read_text(encoding="utf-8"), ticker)
        bucket = market_bucket(ticker)
        if bucket is None:
            unknown.append(ticker)
            continue
        _, chinese_name = _parse_title(path)
        # The result provider is a read-only snapshot lookup, including for
        # archived securities; it must not execute a stock model.
        is_archived = ticker in archived
        try:
            provider_record = provider.current(ticker)
            status, result = map_result(ticker, provider_record)
        except Exception:
            status = "unavailable"
            result = None
        baskets = result.baskets if result is not None else None
        if collect_axes:
            if result is None:
                projections[ticker] = {'status': 'unknown', 'axes': []}
                axis_unknown = True
            else:
                projections[ticker] = project_axes(ticker, provider_record)
        if result is None:
            text = ("未迁移估值" if is_archived and status == "missing" else status_text(status))
        elif result.historical_reference_only:
            text = "历史对照，篮子非当前"
        elif result.basket_error:
            text = "篮子：结果字段无效"
        elif not baskets:
            text = "篮子：未提供"
        elif len(set(baskets.values())) == 1:
            text = f"篮子：篮 {next(iter(baskets.values()))}"
        else:
            text = "篮子：" + " · ".join(f"{name} 篮 {number}" for name, number in sorted(baskets.items()))
        if result is not None:
            notes = [SOURCE_STATUS_MESSAGES[flag] for flag in result.historical_evidence_flags
                     if flag in SOURCE_STATUS_MESSAGES]
            if notes:
                text += " · " + " · ".join(notes)
        if is_archived:
            text = f"已归档，{text}" if result is None else f"已归档 · {text}"
        buckets[bucket].append((identity.ticker, chinese_name, text))

    if collect_axes:
        projected = collect_axis_projection(projections, archived=archived)
        axis_unknown = axis_unknown or any(item['status'] == 'unknown' for item in projections.values())
        historical_axes = any(axis.get('historical_supplement') for projection in projections.values()
                              for axis in projection['axes'])
        axis_metadata = [{'axis_id': a['name'], 'logical_id': '_AXIS.' + a['name'],
                          'axis_type': 'shared_scenario_axis', 'priced_mounts': a['tickers'],
                          'reference_mounts': a['refs']} for a in projected]
        axis_titles = {a['name']: a['title'] for a in projected}
    lines = ["# 标的与轴索引", "", "> 由框架题头及已接纳的计算结果生成；勿手改。", ""]
    for heading, bucket in _SECTIONS:
        if not buckets[bucket]:
            continue
        lines.extend((f"## {heading}", ""))
        for ticker, name, text in sorted(buckets[bucket]):
            suffix = f" — {name}" if bucket != "US" and name else ""
            separator = " · " if suffix else " — "
            lines.append(f"- [{ticker}](./{ticker}.md){suffix}{separator}{text}")
        lines.append("")
    if unknown:
        lines.extend(("## 其他", ""))
        lines.extend(f"- [{ticker}](./{ticker}.md)" for ticker in sorted(unknown))
        lines.append("")
    lines.extend(("## 共享情景轴", ""))
    axis_titles = axis_titles or {}
    by_id = {}
    conflicts = set()
    for axis in axis_metadata:
        if not isinstance(axis, dict):
            axis_unknown = True
            continue
        name, logical_id = axis.get("axis_id"), axis.get("logical_id")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or logical_id != f"_AXIS.{name}":
            axis_unknown = True
            continue
        kind, priced, references = axis.get("axis_type"), axis.get("priced_mounts", []), axis.get("reference_mounts", [])
        if (not isinstance(kind, str) or not kind.strip()
                or not isinstance(priced, list) or not isinstance(references, list)
                or any(not isinstance(ticker, str) or not ticker.strip() for ticker in (*priced, *references))):
            axis_unknown = True
            continue
        # Per-stock `role` and consensus can legitimately differ. The global
        # axis identity and the two membership sets must agree across copies.
        normalized = (logical_id, kind, tuple(sorted(set(priced))), tuple(sorted(set(references))))
        if name in by_id and by_id[name] != normalized:
            conflicts.add(name)
            continue
        by_id[name] = normalized
    for name, axis in sorted(by_id.items()):
        if name in conflicts:
            lines.append(f"- [`_AXIS.{name}`](./axes/{name}.md) — 轴资料冲突，待核对")
            continue
        logical_id, _, priced, references = axis
        title = axis_titles.get(name, name)
        if not isinstance(title, str) or not title.strip():
            raise ValueError("invalid axis metadata")
        groups = []
        if priced:
            groups.append("挂载：" + "、".join(sorted(set(priced))))
        if references:
            groups.append("参照：" + "、".join(sorted(set(references))))
        suffix = " · " + " · ".join(groups) if groups else ""
        lines.append(f"- [`{logical_id}`](./axes/{name}.md) — {title}{suffix}")
    if not axis_metadata:
        lines.append("_（共享情景轴资料待核对）_" if axis_unknown else "_（尚无共享情景轴）_")
    elif axis_unknown:
        lines.append("_部分标的的共享情景轴资料待核对。_")
    if collect_axes and historical_axes:
        lines.append('_部分关系来自历史轴资料，与旧估值的关联尚待核对。_')
    lines.append("")
    # Remaining files are document navigation only: their kind is not inferred
    # from a filename or scraped from their human-readable contents.
    remaining = [path for path in list_axis_paths(frameworks_dir / 'axes') if path.stem not in by_id]
    if remaining:
        lines.extend(('## 其他轴文档（类型待核对）', ''))
        lines.extend(f'- [{path.stem}](./axes/{path.name})' for path in remaining)
        lines.append('')
    return "\n".join(lines)


class _IndexReader:
    """Track whether at least one active result can actually be displayed."""

    def __init__(self, provider, archived):
        self.provider = provider
        self.archived = archived
        self.active_seen = 0
        self.active_results = 0

    def current(self, ticker):
        active = ticker not in self.archived
        self.active_seen += active
        record = self.provider.current(ticker)
        if active and map_result(ticker, record)[1] is not None:
            self.active_results += 1
        return record


def main() -> int:
    archive = FrameworkArchive(ARCHIVE_FILE)
    archived = archive.tickers()
    reader = _IndexReader(open_result_reader(), archived)
    text = build_index_from_results(FRAMEWORKS_DIR, reader, archived=archived)
    if reader.active_seen and not reader.active_results:
        print('没有可读取的未归档估值结果，保留原索引', file=sys.stderr)
        return 2
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
