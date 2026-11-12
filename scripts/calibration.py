#!/usr/bin/env python3
"""Compare saved base valuation bands across active Lynch classifications.

The framework header identifies the stock and its classification. All numeric
values come from the same saved results used by the app and daily digest; this
command does not execute valuation models or infer numbers from prose.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import math
from pathlib import Path
import statistics
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SERVER_ROOT = REPO_ROOT / 'app' / 'server'
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from eval.lib.frameworks import (  # noqa: E402
    FRAMEWORKS_DIR, FrameworkIdentity, list_framework_paths, lynch_cn, parse_framework_identity,
)
from eval.lib.result_service import open_result_reader  # noqa: E402
from eval.lib.result_view import ResultView, SOURCE_STATUS_MESSAGES, map_result, status_text  # noqa: E402


@dataclass(frozen=True)
class _Row:
    identity: FrameworkIdentity
    result: ResultView
    status: str
    growth: float | None

    @property
    def ratios(self) -> tuple[float, float, float]:
        low, middle, high = self.result.anchors
        return low / middle, high / middle, (high - low) / (2 * middle)


def _growth_values(growth):
    if isinstance(growth, dict) and growth.get('unit') == 'RATIO':
        values = growth.get('values')
        if isinstance(values, dict):
            for tier in 'LMH':
                value = values.get(tier)
                if type(value) in (int, float):
                    try:
                        if math.isfinite(value):
                            yield value
                    except OverflowError:
                        pass  # An oversized result is not an observable growth rate.


def _growth_rates(output):
    """Read tier values for the whole company or explicitly reported segments.

    Reference assumptions and optional scenarios can live alongside adopted
    inputs, so never recursively search arbitrary parameter objects.
    """
    parameters = output.get('key_parameters')
    if not isinstance(parameters, dict):
        return
    yield from _growth_values(parameters.get('growth_rate'))
    segments = output.get('segments')
    if isinstance(segments, dict):
        for name in segments:
            segment = parameters.get(name)
            if isinstance(segment, dict):
                yield from _growth_values(segment.get('growth_rate'))


def _notes(row: _Row) -> str:
    status = {'legacy_accepted': '历史沿用', 'local_accepted': '对话采纳',
              'confirmed_snapshot': '已确认历史快照', 'confirmed': '已确认'}.get(row.status, row.status)
    notes = [status]
    if row.result.review_flags or row.result.historical_review_flags:
        notes.append('存在待核事项')
    if (row.result.historical_evidence_differences or
            any(flag not in SOURCE_STATUS_MESSAGES for flag in row.result.historical_evidence_flags)):
        notes.append('历史资料或结果待核')
    notes.extend(SOURCE_STATUS_MESSAGES[flag] for flag in row.result.historical_evidence_flags
                 if flag in SOURCE_STATUS_MESSAGES)
    return '；'.join(dict.fromkeys(notes))


def _basket_text(result: ResultView) -> str:
    if result.basket_error:
        return '无效'
    if not result.baskets:
        return '未提供'
    return '/'.join(str(value) for value in sorted(set(result.baskets.values())))


def _median_nonnegative(values: list[float]) -> float:
    """Keep the usual median while avoiding overflow of two finite values."""
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    low, high = values[mid - 1], values[mid]
    total = low + high
    return total / 2 if math.isfinite(total) else low / 2 + high / 2


def _percent_number(value: float, places: int) -> str:
    """Format the scaled value without overflowing finite inputs."""
    scaled = float(value) * 100
    if math.isfinite(scaled):
        return f'{scaled:.{places}f}'
    return f'{Decimal(str(value)) * 100:.2e}'


def render_calibration(frameworks_dir: Path, provider, *, archived: set[str]) -> tuple[str, int]:
    """Build a report from framework identities and explicitly saved results.

    Return the report and the number of usable active results. Missing or
    invalid individual results remain visible and do not erase other rows.
    """
    rows: list[_Row] = []
    unavailable: list[tuple[str, str]] = []
    skipped = 0
    for path in list_framework_paths(frameworks_dir):
        ticker = path.stem
        if ticker in archived:
            skipped += 1
            continue
        try:
            identity = parse_framework_identity(path.read_text(encoding='utf-8'), ticker)
        except (OSError, ValueError) as exc:
            unavailable.append((ticker, f'框架身份不可读取：{exc}'))
            continue
        try:
            record = provider.current(ticker)
            status, result = map_result(ticker, record)
        except Exception:  # a failed ticker must not hide other saved results
            status, result = 'unavailable', None
        if result is None:
            unavailable.append((ticker, status_text(status)))
            continue
        if result.historical_reference_only:
            unavailable.append((ticker, '仅历史对照，不参与比较'))
            continue
        growth = list(_growth_rates(record['output']))
        row = _Row(identity, result, status, max(growth) if growth else None)
        if not all(math.isfinite(value) for value in row.ratios):
            unavailable.append((ticker, '估值比值超出可表示范围，无法比较'))
            continue
        rows.append(row)

    lines = [f'有效结果 {len(rows)}；不可用 {len(unavailable)}；跳过归档 {skipped}']
    groups: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        groups[row.identity.lynch_primary].append(row)
    for primary, group in sorted(groups.items()):
        group.sort(key=lambda row: row.ratios[0])
        median = statistics.median(row.ratios[0] for row in group)
        lines.append(f'\n{primary}（{lynch_cn(primary)}）— 同类 L/M 中位 {median:.2f}')
        lines.append(f"  {'ticker':12} {'L/M':>6} {'H/M':>6} {'neutral宽/M':>11} {'篮':>8} {'g2%':>8}  次类 · 状态")
        for row in group:
            lm, hm, width = row.ratios
            growth = _percent_number(row.growth, 1) if row.growth is not None else '未提供'
            secondary = row.identity.lynch_secondary or '—'
            width_text = _percent_number(width, 0) + '%'
            lines.append(f'  {row.identity.ticker:12} {lm:6.2f} {hm:6.2f} {width_text:>10}'
                         f' {_basket_text(row.result):>8} {growth:>8}  {secondary} · {_notes(row)}')
    if rows:
        low_ratios = [row.ratios[0] for row in rows]
        high_ratios = [row.ratios[1] for row in rows]
        lines.append(f'\n全仓 {len(rows)} 票：L/M {min(low_ratios):.2f}–{max(low_ratios):.2f}'
                     f'（中位 {statistics.median(low_ratios):.2f}）· H/M '
                     f'{min(high_ratios):.2f}–{max(high_ratios):.2f}'
                     f'（中位 {_median_nonnegative(high_ratios):.2f}）')
    else:
        lines.append('没有可用于比较的估值结果。')
    if unavailable:
        lines.append('\n以下标的未参与比较：')
        lines.extend(f'  {ticker}：{reason}' for ticker, reason in unavailable)
    lines.append('\n只使用基础估值；候选和新业务价值不参与比较。')
    lines.append('g2 为脚本明确标注的已采用长期增长率最高值；没有可识别的值时显示未提供。')
    lines.append('可比市盈率倍数及弹性尚未有统一可识别的输出字段，未提供时不从文档推算。')
    return '\n'.join(lines), len(rows)


def main() -> int:
    try:
        from lynch_reader.archive import ARCHIVE_FILE, FrameworkArchive
        provider = open_result_reader()
    except ModuleNotFoundError as exc:
        if exc.name != 'starlette':
            raise
        print('估值结果读取需要 App 的 Python 环境：app/server/.venv/bin/python scripts/calibration.py',
              file=sys.stderr)
        return 1
    report, count = render_calibration(FRAMEWORKS_DIR, provider, archived=FrameworkArchive(ARCHIVE_FILE).tickers())
    print(report)
    return 0 if count else 1


if __name__ == '__main__':
    raise SystemExit(main())
