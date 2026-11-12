#!/usr/bin/env python3
"""Daily: quote → zone from framework L/M/H → write bands → Feishu notify.

Always sends a daily digest. If any ticker's zone changed vs previous artifact
(and previous_zone was not null), the message @所有人.

Does NOT write frameworks/*.md.
"""
from __future__ import annotations

import argparse
import logging
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SERVER_ROOT = REPO_ROOT / 'app' / 'server'
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from eval.lib.bands_io import build_record, read_previous, write_bands  # noqa: E402
from eval.lib.feishu import send_post  # noqa: E402
from eval.lib.frameworks import (FRAMEWORKS_DIR, FrameworkMeta, list_framework_paths,
                                 parse_framework_identity)  # noqa: E402
from eval.lib.price import fetch_quotes  # noqa: E402
from eval.lib.result_view import SOURCE_STATUS_MESSAGES, map_result, status_text  # noqa: E402
from eval.lib.result_service import open_result_reader  # noqa: E402
from lynch_reader.archive import ARCHIVE_FILE, FrameworkArchive  # noqa: E402
# candidate_zone / credited_option_zone are re-exported: the digest tests call them from here.
from eval.lib.zones import (  # noqa: E402, F401
    candidate_zone,
    credited_option_zone,
    zone_bound_edges,
    zone_emoji,
    zone_label,
    zone_of,
)


log = logging.getLogger("daily_bands")
TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class Row:
    meta: FrameworkMeta
    price: float
    zone: str
    previous_zone: str | None
    crossed: bool
    notes: list[str]
    record: dict
    option_zone: str | None = None
    previous_option_zone: str | None = None
    option_crossed: bool = False
    candidate_zone: str | None = None
    previous_candidate_zone: str | None = None
    candidate_crossed: bool = False
    # Rate sensitivities come from the stored result; they never trigger a cross.
    rf: dict | None = None

    @property
    def any_crossed(self) -> bool:
        return self.crossed or self.option_crossed or self.candidate_crossed


@dataclass(frozen=True)
class ResultMeta:
    ticker: str
    name: str
    currency: str
    L: float
    M: float
    H: float
    option_band: tuple[float, float, float] | None
    v_opt: float | None
    candidate: tuple[float, float, float] | None
    candidate_label: str | None
    candidate_status: str | None
    option_status: str | None
    review_flags: tuple[str, ...]
    historical_evidence_flags: tuple[str, ...]
    historical_review_flags: tuple[str, ...]
    historical_option_status: str | None
    fund_reference_status: str | None
    historical_fund_reference_status: str | None
    legacy_accepted: bool
    archived: bool = False
    option_label: str | None = None
    historical_reference_bands: tuple[dict, ...] = ()
    historical_reference_only: bool = False
    historical_reference_double_counting: bool = False
    valuation_as_of: str | None = None


@dataclass(frozen=True)
class ArchivedRow:
    ticker: str
    name: str
    price: float | None
    currency: str | None
    status: str = "missing"
    meta: ResultMeta | None = None
    rf: dict | None = None
    rate_error: bool = False
    notes: tuple[str, ...] = ()
    record: dict | None = None

    @property
    def any_crossed(self) -> bool:
        return False


def _quote(quote):
    price = quote if type(quote) in (int, float) else getattr(quote, "last", None)
    try:
        valid = type(price) in (int, float) and math.isfinite(price) and price > 0
    except OverflowError:
        valid = False
    currency = getattr(quote, "currency", None)
    if currency not in ("USD", "CNY", "HKD", "TWD"):
        currency = None
    return (price if valid else None), getattr(quote, "status", None), currency


def build_rows_from_results(identities, provider, quotes, previous, *, archived, price_source='injected_quote'):
    """Build the daily view from injected quotes, observations and saved results.

    The caller supplies identities only; numeric values come from the provider.
    Provider.current is a read-only lookup and must never execute any model,
    including for archived securities.
    """
    rows, errors = [], []
    for identity in identities:
        ticker = identity.ticker
        is_archived = ticker in archived
        price, quote_status, quote_currency = _quote(quotes.get(ticker))
        if price is None and not is_archived:
            errors.append(f'{ticker}: 未取得有效报价')
        try:
            status, result = map_result(ticker, provider.current(ticker))
        except Exception:  # One unavailable result must not hide other rows.
            if is_archived:
                rows.append(ArchivedRow(ticker, identity.name, price, quote_currency, status="unavailable"))
            else:
                errors.append(f"{ticker}: 估值结果暂不可读取")
            continue
        if result is None:
            if is_archived:
                rows.append(ArchivedRow(ticker, identity.name, price, quote_currency, status=status))
            else:
                errors.append(f"{ticker}: {status_text(status)}")
            continue
        L, M, H = result.anchors
        meta = ResultMeta(ticker, identity.name, result.currency, L, M, H, result.option_band,
                          result.option_value, result.candidate, result.candidate_label,
                          result.candidate_status, result.option_status, result.review_flags,
                          result.historical_evidence_flags, result.historical_review_flags,
                          result.historical_option_status, result.fund_reference_status,
                          result.historical_fund_reference_status, status == "legacy_accepted", is_archived,
                          result.option_label, result.historical_reference_bands,
                          result.historical_reference_only, result.historical_reference_double_counting,
                          result.valuation_as_of)
        rf = None
        if result.rate_consensus is not None and result.rate_pressure is not None:
            rf = {"stale": False, "delta_cons": result.delta_cons, "rf_cap": result.rf_cap,
                  "consensus": result.rate_consensus, "pressure": result.rate_pressure}
        if price is None or result.historical_reference_only:
            # A historical comparison has no current zone and must never
            # feed the next comparison or trigger a crossing.  Record only a
            # reset marker, never its historical anchors as a current band.
            marker = ({'ticker': ticker, 'as_of': _as_of_iso(), 'zone': None,
                       'option_zone': None, 'candidate_zone': None,
                       'notes': ['historical_reference_only']}
                      if result.historical_reference_only and not is_archived else None)
            rows.append(ArchivedRow(ticker, identity.name, price, result.currency, status=status,
                                    meta=meta, rf=rf, rate_error=result.rate_error, record=marker))
            continue
        zone = zone_of(price, L, M, H)
        option_zone = zone_of(price, *result.option_band) if result.option_band is not None else None
        cand_zone = zone_of(price, *result.candidate) if result.candidate is not None else None
        prior = previous.get(ticker)
        prior = prior if isinstance(prior, dict) else {}
        prev = prior.get("zone") if isinstance(prior.get("zone"), str) else None
        prev_option = prior.get("option_zone") if isinstance(prior.get("option_zone"), str) else None
        prev_cand = prior.get("candidate_zone") if isinstance(prior.get("candidate_zone"), str) else None
        notes = ["first_run"] if prev is None else []
        record = build_record(
            ticker=ticker, as_of=_as_of_iso(), price=price, currency=result.currency,
            L=L, M=M, H=H, zone=zone, previous_zone=prev,
            option_zone=option_zone, previous_option_zone=prev_option,
            candidate_zone=cand_zone, previous_candidate_zone=prev_cand, notes=notes,
            formula_id="valuation_result_v1",
            inputs_used={"price_source": price_source, "price_field": "last",
                         "quote_status": quote_status, "anchors_source": status,
                         "valuation_ref": result.result_ref, "rate_format": result.rate_format},
        )
        if result.rate_error:
            errors.append(f"{ticker}: 利率情景结果无效")
        if result.basket_error:
            errors.append(f"{ticker}: 估值篮子字段无效")
        rows.append(Row(meta=meta, price=price, zone=zone, previous_zone=prev,
                        crossed=zone_changed(prev, zone), notes=notes, record=record,
                        option_zone=option_zone, previous_option_zone=prev_option,
                        option_crossed=zone_changed(prev_option, option_zone),
                        candidate_zone=cand_zone, previous_candidate_zone=prev_cand,
                        candidate_crossed=zone_changed(prev_cand, cand_zone), rf=rf))
    return rows, errors


def _as_of_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# A-share exchange-traded funds (SH 5xxxxx, SZ 15xxxx–18xxxx) tick at 0.001, stocks at 0.01.
_A_SHARE_FUND = re.compile(r"^(5\d{5}\.SH|1[5-8]\d{4}\.SZ)$")


def price_decimals(ticker: str) -> int:
    """Decimals below 100 for `ticker`'s prices: its quote tick, so a 0.001 move shows."""
    return 3 if _A_SHARE_FUND.match(ticker) else 2


def _fmt_num(price: float, dp: int = 2) -> str:
    """Same precision as Feishu price display (≥100 → 1dp, else `dp`)."""
    if price >= 100:
        return f"{price:.1f}"
    return f"{price:.{dp}f}"


def _fmt_price(price: float, currency: str, dp: int = 2) -> str:
    return f"{_fmt_num(price, dp)} {currency}"


def _fmt_anchor(x: float, dp: int = 2) -> str:
    """`dp` decimals, trailing zeros stripped (`27.2`, `246.44`, `113`)."""
    s = f"{x:.{dp}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def format_lmh(L: float, M: float, H: float, dp: int = 2) -> str:
    return f"L/M/H = {_fmt_anchor(L, dp)}/{_fmt_anchor(M, dp)}/{_fmt_anchor(H, dp)}"


def format_zone_bounds(zone: str, L: float, M: float, H: float, dp: int = 2) -> str:
    """Current-zone lo/hi in AGENTS bracket notation (`[A, B)` / `P < L`)."""
    shape, lo, hi = zone_bound_edges(zone, L, M, H)
    if shape == "open_below":
        assert hi is not None
        return f"P < {_fmt_anchor(hi, dp)}"
    if shape == "open_above":
        assert lo is not None
        return f"P > {_fmt_anchor(lo, dp)}"
    assert lo is not None and hi is not None
    lo_s, hi_s = _fmt_anchor(lo, dp), _fmt_anchor(hi, dp)
    if shape == "half_open":
        return f"[{lo_s}, {hi_s})"
    if shape == "closed":
        return f"[{lo_s}, {hi_s}]"
    raise ValueError(f"unknown shape: {shape}")


def zone_changed(previous: str | None, current: str | None) -> bool:
    """True only when both sides are known and differ. None = first observation, no alert."""
    return previous is not None and current is not None and previous != current


def _short_name(meta: FrameworkMeta) -> str:
    """Title segment before ' / ' (中文名优先)."""
    name = (meta.name or "").strip()
    if " / " in name:
        name = name.split(" / ", 1)[0].strip()
    return name or meta.ticker


def format_pool_lines(r: Row) -> list[str]:
    """Per-ticker Feishu block.

    🟡 603993.SH 洛阳钼业 19.01 CNY
    「中性」 [16.57, 21.44) · L/M/H = 13.51/19.63/23.24

    Tickers with V_opt > 0 get a third line on the credited band:
    「乐观」 +V(66.6) [71.17, 113] · L/M/H = 27.2/71.17/113
    Tickers carrying a candidate band get one more, last:
    「叙事溢价」 候选(FY28·Piper单家) P > 16.29 · L/M/H = 7.23/15.54/16.29
    """
    if isinstance(r, ArchivedRow):
        name = r.name.split(" / ", 1)[0].strip()
        quote = "暂无报价" if r.price is None else _fmt_num(r.price, price_decimals(r.ticker))
        if r.price is not None and r.currency is not None:
            quote += f" {r.currency}"
        lines = [f"⚪ {r.ticker} {name} {quote}"]
        if r.meta is None:
            reason = "未迁移估值" if r.status == "missing" else status_text(r.status)
            return lines + [f"已归档，{reason}"]
        dp = price_decimals(r.ticker)
        if r.meta.archived:
            lines.append("已归档")
        lines.append(format_lmh(r.meta.L, r.meta.M, r.meta.H, dp))
        if r.meta.option_band is not None:
            label = r.meta.option_label or (f"+V({_fmt_anchor(r.meta.v_opt, dp)})" if r.meta.v_opt is not None else "新业务")
            lines.append(f"{label} · {format_lmh(*r.meta.option_band, dp)}")
        if r.meta.candidate is not None:
            lines.append(f"候选({r.meta.candidate_label}) · {format_lmh(*r.meta.candidate, dp)}")
        lines.extend(_result_cautions(r.meta))
        if r.rf:
            lines.append(f"利率·共识({r.rf['delta_cons'] * 1e4:+.0f}bp) · {format_lmh(*r.rf['consensus'], dp)}")
            lines.append(f"利率·压力±{r.rf['rf_cap'] * 1e4:.0f}bp · {format_lmh(*r.rf['pressure'], dp)}")
        if r.rate_error:
            lines.append("利率情景结果无效")
        return lines
    title = r.meta.ticker
    if not r.meta.ticker.endswith(".US"):
        title = f"{r.meta.ticker} {_short_name(r.meta)}"
    emoji = zone_emoji(r.zone)
    dp = price_decimals(r.meta.ticker)
    header = f"{emoji} {title} {_fmt_price(r.price, r.meta.currency, dp)}"
    bounds = format_zone_bounds(r.zone, r.meta.L, r.meta.M, r.meta.H, dp)
    lines = [
        header,
        f"「{zone_label(r.zone)}」 {bounds} · {format_lmh(r.meta.L, r.meta.M, r.meta.H, dp)}",
    ]
    if getattr(r.meta, "archived", False):
        lines.append("已归档")
    band = r.meta.option_band
    v_opt = r.meta.v_opt
    if band is not None and (v_opt is None or v_opt > 0) and r.option_zone:
        L2, M2, H2 = band
        ob = format_zone_bounds(r.option_zone, L2, M2, H2, dp)
        option_label = getattr(r.meta, 'option_label', None) or (f"+V({_fmt_anchor(v_opt, dp)})" if v_opt is not None else "新业务")
        lines.append(
            f"「{zone_label(r.option_zone)}」 {option_label} {ob} · {format_lmh(L2, M2, H2, dp)}"
        )
    if r.meta.candidate is not None and r.candidate_zone:
        L3, M3, H3 = r.meta.candidate
        cb = format_zone_bounds(r.candidate_zone, L3, M3, H3, dp)
        lines.append(
            f"「{zone_label(r.candidate_zone)}」 候选({r.meta.candidate_label}) "
            f"{cb} · {format_lmh(L3, M3, H3, dp)}"
        )
    lines.extend(_result_cautions(r.meta))
    lines.extend(format_rf_lines(r))
    return lines


def _result_cautions(meta) -> list[str]:
    lines = []
    if hasattr(meta, 'valuation_as_of'):
        label = '历史估值基准' if getattr(meta, 'historical_reference_only', False) else '估值基准'
        day = meta.valuation_as_of
        lines.append(f'{label}：{day}' if day else f'{label}日期待核')
    if (getattr(meta, 'historical_reference_double_counting', False)
            or 'historical_double_counting' in getattr(meta, 'review_flags', ())
            or 'historical_double_counting' in getattr(meta, 'historical_review_flags', ())):
        lines.append('旧估值存在重复计价，仅供历史对照')
    if ((getattr(meta, 'historical_reference_only', False)
         or 'historical_reference_only' in getattr(meta, 'review_flags', ()))
            and not getattr(meta, 'historical_reference_double_counting', False)
            and 'historical_double_counting' not in getattr(meta, 'review_flags', ())):
        lines.append('旧估值仅供历史对照，不作为当前价带')
    if getattr(meta, "review_flags", ()) or getattr(meta, "historical_review_flags", ()):
        lines.append("估值假设有待核对事项，请查看框架说明")
    if getattr(meta, "legacy_accepted", False):
        lines.append("历史框架沿用，计算尚待复核")
    for reference in getattr(meta, 'historical_reference_bands', ()):
        kind = '候选' if reference['kind'] == 'candidate' else '新业务'
        label = f"（{reference['label']}）" if reference.get('label') else ''
        anchors = reference['anchors']
        values = ' / '.join(str(anchors[tier]) if tier in anchors else '—' for tier in 'LMH')
        recorded_unit = reference.get('unit')
        unit_match = (re.fullmatch(r'(CNY|USD|HKD|TWD)_PER_(SHARE|FUND_UNIT|UNIT)', recorded_unit)
                      if isinstance(recorded_unit, str) else None)
        if unit_match:
            currencies = {'CNY': '元', 'USD': '美元', 'HKD': '港元', 'TWD': '新台币'}
            denominators = {'SHARE': '股', 'FUND_UNIT': '份', 'UNIT': '单位'}
            unit = f'{currencies[unit_match[1]]}/{denominators[unit_match[2]]}'
        else:
            unit = '单位待核'
        lines.append(f'历史{kind}{label}：{values} {unit} · 仅供对照，当前采用状态待核')
    evidence_messages = {
        **SOURCE_STATUS_MESSAGES,
        "candidate_unknown": "旧材料的候选价带是否完整仍待核对",
        "option_unknown": "旧材料的新业务价带是否完整仍待核对",
        "candidate_absence_attested": "候选价带的缺省由历史材料人工核对",
        "option_absence_attested": "新业务价带的缺省由历史材料人工核对",
        "option_anchors_unknown": "旧材料的新业务价带档位仍待核对",
        "anchors_unit_unknown": "历史估值区间的计价单位待核对",
        "candidate_unit_unknown": "历史候选价带的计价单位待核对",
        "option_unit_unknown": "历史新业务价带的计价单位待核对",
        "option_value_unit_unknown": "历史新业务价值的单位待核对",
        "option_value_unknown": "历史新业务价值的数额仍待核对",
        "option_value_unit_incompatible": "历史新业务价值与价带的计价口径不同",
        "review_flags_unknown": "旧框架的风险提示是否完整仍待核对",
        "option_status_unknown": "旧材料的新业务状态仍待核对",
        "display_precision_attested": "历史价格按原资料的显示精度对照",
        "price_unit_attested": "历史价格单位按资料说明对照",
        "historical_values_differ": "历史材料与当前估值结果有差异，请查看框架说明",
    }
    evidence_flags = getattr(meta, "historical_evidence_flags", ())
    for flag in evidence_flags:
        if flag == 'precision_unknown' and any(known in evidence_flags for known in
                                               ('historical_option_mid_high_two_decimals', 'historical_base_two_decimals',
                                                'historical_option_mid_high_one_decimal')):
            lines.append('其他历史数字的精度仍待核')
        else:
            lines.append(evidence_messages.get(flag, "历史材料部分细节待核对"))
    candidate_status = getattr(meta, "candidate_status", None)
    if candidate_status == "unavailable":
        lines.append("候选价带暂不可用，请查看框架说明")
    elif candidate_status not in (None, "conditional"):
        lines.append("候选估值需要核对，请查看框架说明")
    option_status = getattr(meta, "option_status", None)
    if option_status == "unavailable":
        lines.append("新业务估值暂不可用，请查看框架说明")
    elif option_status not in (None, "conditional"):
        lines.append("新业务估值需要核对，请查看框架说明")
    historical_option_status = getattr(meta, "historical_option_status", None)
    if historical_option_status not in (None, "conditional") and historical_option_status != option_status:
        lines.append("旧材料的新业务估值状态仍需核对")
    fund_status = getattr(meta, "fund_reference_status", None)
    historical_fund_status = getattr(meta, "historical_fund_reference_status", None)
    if (fund_status not in (None, "available", "confirmed")
            or historical_fund_status not in (None, "available", "confirmed")):
        lines.append("基金穿透对照存在待核对事项")
    return lines


def format_rf_lines(r: Row) -> list[str]:
    """Two r_f reading lines (valuation-rules「利率轴」): consensus Δ, then reverse pressure.

    「叙事溢价」 利率·共识(+75bp) P > 312.63 · L/M/H = 215.7/264.9/312.63
    「中性」 利率·压力±50bp [266.24, 355.63) · L/M/H = 225.87/306.6/404.67
    """
    if r.rf is None:
        return []
    if r.rf["stale"]:
        return [f"利率读数过期（利率轴或锚点已变，重跑 frameworks/eval/{r.meta.ticker}.py）"]
    dp = price_decimals(r.meta.ticker)
    out = []
    for label, band in (
        (f"利率·共识({r.rf['delta_cons'] * 1e4:+.0f}bp)", r.rf["consensus"]),
        (f"利率·压力±{r.rf['rf_cap'] * 1e4:.0f}bp", r.rf["pressure"]),
    ):
        z = zone_of(r.price, *band)
        out.append(f"「{zone_label(z)}」 {label} {format_zone_bounds(z, *band, dp)} · {format_lmh(*band, dp)}")
    return out


def _join_ticker_blocks(blocks: list[list[str]]) -> list[str]:
    lines: list[str] = []
    for i, block in enumerate(blocks):
        if i:
            lines.append("")
        lines.extend(block)
    return lines


def build_daily_post(rows: list[Row], *, as_of_local: str) -> tuple[str, list[str]]:
    """Rich-text daily digest lines (Feishu post body)."""
    title = f"lynch 价带日报 · {as_of_local}"
    lines = _join_ticker_blocks([format_pool_lines(r) for r in rows])
    first_run = sum(1 for r in rows if "first_run" in r.notes)
    if first_run:
        lines.append("")
        lines.append(
            f"⚠️ first_run {first_run}/{len(rows)}——本机无历史 bands，跨带告警本次不会触发"
        )
    return title, lines


def build_cross_post(crosses: list[Row], *, as_of_local: str) -> tuple[str, list[str]]:
    """Zone-change alert lines; caller @所有人 via send_post.

    Base-deck and +V-deck crosses are independent: either one (or both) emits
    a transition line under the same snapshot block.
    """
    title = f"lynch 跨带 · {as_of_local}"
    blocks: list[list[str]] = []
    for r in crosses:
        if isinstance(r, ArchivedRow):
            continue
        block = format_pool_lines(r)
        if r.crossed:
            prev = r.previous_zone or "?"
            block.append(f"「{zone_label(prev)}」 → 「{zone_label(r.zone)}」")
        if r.option_crossed:
            prev_o = r.previous_option_zone or "?"
            cur_o = r.option_zone or "?"
            option_label = getattr(r.meta, 'option_label', None) or (
                "新业务" if isinstance(r.meta, ResultMeta) and r.meta.v_opt is None else "+V")
            block.append(f"{option_label} 「{zone_label(prev_o)}」 → 「{zone_label(cur_o)}」")
        if r.candidate_crossed:
            prev_c = r.previous_candidate_zone or "?"
            cur_c = r.candidate_zone or "?"
            label = getattr(r.meta, 'candidate_label', None)
            block.append(f"候选({label}) 「{zone_label(prev_c)}」 → 「{zone_label(cur_c)}」" if label
                         else f"候选 「{zone_label(prev_c)}」 → 「{zone_label(cur_c)}」")
        blocks.append(block)
    return title, _join_ticker_blocks(blocks)


def _send_post(title: str, lines: list[str], *, at_all: bool, dry_run: bool) -> dict:
    result = send_post(title, lines, at_all=at_all, dry_run=dry_run)
    if result.get("skipped_reason") == "missing_webhook" and not dry_run:
        log.error("Feishu webhook missing (set FEISHU_ALERT_WEBHOOK or reuse invest policies)")
    elif not result.get("ok"):
        log.error("Feishu send failed: %s", result)
    else:
        log.info(
            "feishu ok dry_run=%s at_all=%s title=%s",
            result.get("dry_run"),
            result.get("at_all"),
            title,
        )
    return result


def _report_failure(errors: list[str], *, dry_run: bool, notify: bool) -> int:
    """Report a failed whole run without producing a successful observation."""
    title = f"lynch 日报失败 · {datetime.now(TZ).strftime('%Y-%m-%d %H:%M %Z')}"
    lines = ['日报未生成', '【失败】', *(f'• {error}' for error in errors)]
    print(title)
    print('\n'.join(lines))
    if notify or dry_run:
        _send_post(title, lines, at_all=False, dry_run=dry_run or not notify)
    return 2



def run(*, dry_run: bool, notify: bool, bands_dir: Path | None) -> int:
    # Only framework identities come from prose; all bands come from the
    # same persisted result reader used by the app and index.
    identities, errors = [], []
    for path in list_framework_paths(FRAMEWORKS_DIR):
        try:
            identities.append(parse_framework_identity(path.read_text(encoding='utf-8'), path.stem))
        except (OSError, ValueError) as exc:
            errors.append(f'{path.stem}: 框架题头不可读取: {exc}')
    try:
        archived = FrameworkArchive(ARCHIVE_FILE).tickers()
    except (OSError, ValueError) as exc:
        log.error('archive unavailable: %s', exc)
        return _report_failure(['归档清单不可读取'], dry_run=dry_run, notify=notify)
    symbols = [identity.ticker for identity in identities]
    try:
        quotes = fetch_quotes(symbols, allow_missing=True)
    except Exception as exc:  # noqa: BLE001 — frozen bands remain usable without quotes
        log.error('quote fetch failed: %s', exc)
        quotes = {}
        errors.append('报价整体不可用')
    previous = {ticker: read_previous(ticker, bands_dir) for ticker in symbols}
    rows, result_errors = build_rows_from_results(identities, open_result_reader(), quotes, previous,
                                                  archived=archived, price_source='longbridge')
    errors.extend(result_errors)
    if not dry_run:
        for row in rows:
            if row.record is not None:
                write_bands(row.record, bands_dir)

    if not rows and errors:
        log.error("no rows computed: %s", "; ".join(errors))
        return _report_failure(errors, dry_run=dry_run, notify=notify)

    crosses = [r for r in rows if r.any_crossed]
    as_of_local = datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z")
    daily_title, daily_lines = build_daily_post(rows, as_of_local=as_of_local)
    if errors:
        daily_lines.append("")
        daily_lines.append("【失败】")
        daily_lines.extend(f"• {e}" for e in errors)

    log.info("computed %d tickers, crosses=%d", len(rows), len(crosses))
    print(daily_title)
    print("\n".join(daily_lines))
    if crosses:
        cross_title, cross_lines = build_cross_post(crosses, as_of_local=as_of_local)
        print()
        print(cross_title)
        print("\n".join(cross_lines))
    else:
        cross_title = ""
        cross_lines: list[str] = []

    exit_code = 1 if errors else 0

    if notify or dry_run:
        # Always: daily post (no @all). Separately: cross post with @所有人.
        r1 = _send_post(
            daily_title, daily_lines, at_all=False, dry_run=dry_run or not notify
        )
        if notify and not dry_run and not r1.get("ok"):
            return 2
        if crosses:
            r2 = _send_post(
                cross_title, cross_lines, at_all=True, dry_run=dry_run or not notify
            )
            if notify and not dry_run and not r2.get("ok"):
                return 2

    return exit_code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="do not write bands JSON; Feishu payload logged only",
    )
    p.add_argument(
        "--no-notify",
        action="store_true",
        help="skip Feishu (still write bands unless --dry-run)",
    )
    p.add_argument(
        "--bands-dir",
        type=Path,
        default=None,
        help="override output/bands directory",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(
        dry_run=args.dry_run,
        notify=not args.no_notify,
        bands_dir=args.bands_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
