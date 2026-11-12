"""Validate a trusted result-provider record for numeric consumers.

The provider owns confirmation and provenance. This module does not run
models, retrieve files, infer an authorization, or read Markdown valuations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from math import isfinite
from typing import Any

_STATUS_TEXT = {
    "missing": "待迁移", "unconfirmed": "估值结果待确认", "stale": "估值依据已变化",
    "invalid": "估值结果无效", "changed": "估值结果正在更新",
    "proof_unavailable": "估值结果暂不可验证", "unavailable": "估值结果暂不可读取",
}

SOURCE_STATUS_MESSAGES = {
    'historical_script_missing': '旧脚本版本尚未关联，历史结果待核',
    'historical_recorded_source_changed': '历史快照记录的来源已变化，待核对',
    'historical_candidate_upgrade_pending': '候选升级条件已触发，仍待复核',
    'historical_low_display_difference': '旧文保守档与脚本显示值有差异，详见文档',
    'historical_option_cost_basis_difference': '额外情景已扣首飞前费用，旧日报口径不同',
    'historical_option_mid_high_two_decimals': '额外情景中性、乐观档沿用旧输出的两位小数',
    'historical_conditional_funding': '历史估值有条件采用，资金投入和来源仍待核',
    'historical_mid_forecast_weakened': '旧中性盈利预测已被财报削弱，待重新确定',
    'historical_base_two_decimals': '基础价带沿用旧输出的两位小数',
    'historical_option_mid_high_one_decimal': '额外情景中性、乐观档沿用旧输出的一位小数',
    'historical_separate_syntax_error': '另一份旧日志记录语法错误，是否同次运行待核',
    'historical_earlier_failed_log': '另有较早旧日志记录核对失败，与本次记录的关系待核',
    'historical_run_log_incomplete': '历史运行日志不完整或有冲突，待核',
    'historical_financial_basis_unknown': '历史估值的财务基准日期待核',
    'historical_base_four_decimals': '基础三档沿用旧脚本输出的四位小数',
    'historical_option_all_two_decimals': '额外情景沿用旧脚本输出的两位小数',
    'historical_candidate_reference_conflict': '旧候选与现有独立对照口径不同，待核',
    'current_framework_funding_gap_unquantified': '当前框架提示扩产资金缺口未量化，请结合文档判断',
    'confirmed_snapshot_unverified': '显示已确认的历史快照，当前执行证明不可用',
    'valuation_source_unavailable': '估值来源暂不可核验，当前展示历史快照',
    'valuation_source_changed': '估值来源已变化，当前展示历史快照',
    'valuation_source_previously_unavailable': '接纳历史快照时，估值来源曾不可核验',
    'valuation_source_previously_changed': '接纳历史快照时，估值来源曾发生变化',
    'valuation_source_previously_ambiguous': '接纳时的来源检查不能区分变化和读取失败',
    'external_history_source_changed': '外部历史数据来源已变化，旧结果待复算',
    'external_history_source_unavailable': '外部历史数据来源暂不可读取',
    'valuation_runtime_unavailable': '计算环境暂不可用',
    'valuation_runtime_changed': '计算环境已变化',
    'peer_result_unverified': '引用的其他标的当前估值尚未核对',
    'execution_unverified': '计算来源尚未独立核验',
}


def status_text(status: str) -> str:
    return _STATUS_TEXT.get(status, "估值结果不可用")


@dataclass(frozen=True)
class ResultView:
    ticker: str
    currency: str
    anchors: tuple[float, float, float]
    candidate: tuple[float, float, float] | None
    candidate_label: str | None
    candidate_status: str | None
    option_band: tuple[float, float, float] | None
    option_value: float | None
    option_status: str | None
    review_flags: tuple[str, ...]
    historical_evidence_flags: tuple[str, ...]
    historical_evidence_states: dict[str, str] | None
    historical_evidence_differences: tuple[dict[str, Any], ...]
    historical_evidence_source: dict[str, str] | None
    historical_review_flags: tuple[str, ...]
    historical_option_status: str | None
    historical_fund_reference_status: str | None
    fund_reference_status: str | None
    rate_consensus: tuple[float, float, float] | None
    rate_pressure: tuple[float, float, float] | None
    delta_cons: float | None
    rf_cap: float | None
    rate_error: bool
    rate_format: str | None
    baskets: dict[str, int] | None
    basket_error: bool
    result_ref: str
    historical_reference_bands: tuple[dict[str, Any], ...]
    option_label: str | None
    axis_metadata: dict[str, Any] | None
    reference_only: bool = False
    reference_double_counting: bool = False
    valuation_as_of: str | None = None

    @property
    def historical_reference_only(self) -> bool:
        # Historical comparison flags may describe an older, corrected model.
        # Only the displayed result's own flag makes its bands reference-only.
        return (self.reference_only or self.reference_double_counting
                or any(flag in self.review_flags for flag in ('historical_reference_only', 'historical_double_counting')))

    @property
    def historical_reference_double_counting(self) -> bool:
        return self.reference_double_counting or 'historical_double_counting' in self.review_flags

    @property
    def L(self):
        return self.anchors[0]

    @property
    def M(self):
        return self.anchors[1]

    @property
    def H(self):
        return self.anchors[2]

    @property
    def v_opt(self):
        return self.option_value


def _number(value: Any) -> bool:
    if type(value) not in (float, int):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False


def _valuation_date(value: Any) -> str | None:
    """Only expose a date explicitly present on this result, never infer one."""
    if not isinstance(value, str):
        return None
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _units(value: dict, permitted: set[str], collected: list[str]) -> None:
    if "unit" in value:
        collected.append(value["unit"])
    if "units" in value:
        units = value["units"]
        if not isinstance(units, dict) or set(units) != {"price"}:
            raise ValueError("invalid band unit mapping")
        collected.append(units["price"])
    if any(not isinstance(unit, str) or unit not in permitted for unit in collected):
        raise ValueError("invalid price unit")


def _band(value: Any, permitted: set[str], collected: list[str]) -> tuple[float, float, float]:
    if (not isinstance(value, dict) or not {"L", "M", "H"} <= set(value)
            or set(value) - {"L", "M", "H", "unit", "units"}):
        raise ValueError("invalid price band")
    _units(value, permitted, collected)
    low, middle, high = (value[key] for key in ("L", "M", "H"))
    if (not all(_number(v) for v in (low, middle, high)) or not 0 <= low < middle < high
            or not _number(low + middle) or not _number(middle + high)):
        raise ValueError("invalid price ladder")
    return low, middle, high


def _rate_sensitivity(output: dict, currency: str, permitted: set[str], price_units: list[str]):
    if output.get("rate") is not None:
        return None, None, None, None, True, None
    rate = output.get("rate_sensitivity")
    if rate is None:
        return None, None, None, None, False, None
    try:
        if not isinstance(rate, dict):
            raise ValueError("invalid rate sensitivity")
        if rate.get("price_currency", currency) != currency:
            raise ValueError("rate price currency differs")
        unit = rate.get("price_unit")
        if unit is not None and unit not in ("per_share", "per_fund_unit"):
            raise ValueError("invalid rate price unit")
        if unit == "per_share" and any(u.endswith(("_PER_UNIT", "_PER_FUND_UNIT")) for u in price_units):
            raise ValueError("rate is per share but base is per fund unit")
        if unit == "per_fund_unit" and any(u.endswith("_PER_SHARE") for u in price_units):
            raise ValueError("rate is per fund unit but base is per share")
        inputs = rate["inputs"]
        delta, cap = inputs["delta_cons"], inputs["rf_cap"]
        if not _number(delta) or not _number(cap) or cap <= 0:
            raise ValueError("invalid rate assumptions")
        rate_units = list(price_units)
        consensus = _band(rate["consensus"], permitted, rate_units)
        pressure = _band(rate["pressure"], permitted, rate_units)
        if len(set(rate_units)) > 1:
            raise ValueError("rate unit differs from base")
        return consensus, pressure, delta, cap, False, "rate_sensitivity"
    except (KeyError, TypeError, ValueError):
        return None, None, None, None, True, "rate_sensitivity"


def map_result(ticker: str, record: Any) -> tuple[str, ResultView | None]:
    """Normalize only a current provider-owned confirmation or legacy acceptance."""
    if not isinstance(record, dict):
        return "invalid", None
    status = record.get("status")
    if status not in ("confirmed", "confirmed_snapshot", "legacy_accepted", "local_accepted"):
        return (status if isinstance(status, str) and status in {"missing", "unconfirmed", "stale", "invalid", "changed", "proof_unavailable", "unavailable"}
                else "invalid"), None
    if status in ("confirmed", "confirmed_snapshot"):
        ref = record.get("review_id")
    else:
        ref = record.get("batch_id") if record.get("origin") == "local_operator_attestation" else None
    if not isinstance(ref, str) or not ref.strip():
        return "invalid", None
    try:
        reference_only = record.get('reference_only', False)
        reference_double_counting = record.get('reference_double_counting', False)
        if type(reference_only) is not bool or type(reference_double_counting) is not bool:
            raise ValueError('invalid current reference state')
        evidence_flags = record.get("historical_evidence_flags", []) if status in ("legacy_accepted", "confirmed_snapshot", "local_accepted") else []
        if not isinstance(evidence_flags, list) or any(not isinstance(flag, str) or not flag.strip() for flag in evidence_flags):
            raise ValueError("invalid historical evidence flags")
        evidence_flags = list(evidence_flags)
        evidence_states = None
        differences = []
        evidence_source = None
        historical_flags = []
        historical_option_status = None
        historical_fund_status = None
        reference_bands = []
        if status == "legacy_accepted":
            comparison = record.get("historical_comparison")
            if not isinstance(comparison, dict):
                raise ValueError("missing historical comparison")
            evidence_states = {}
            for key in ("candidate", "option"):
                entry = comparison.get(key)
                if not isinstance(entry, dict) or entry.get("state") not in ("present", "absent", "unknown"):
                    raise ValueError("invalid historical comparison state")
                evidence_states[key] = entry["state"]
            differences = record.get("historical_evidence_differences", [])
            if (not isinstance(differences, list) or any(not isinstance(item, dict)
                    or not isinstance(item.get("field"), str) or not isinstance(item.get("kind"), str)
                    or "historical" not in item or "current" not in item for item in differences)):
                raise ValueError("invalid historical differences")
            json.dumps(differences, allow_nan=False)
            differences = list(differences)
            evidence_source = record.get("historical_evidence_source")
            if (not isinstance(evidence_source, dict) or not isinstance(evidence_source.get("reference"), str)
                    or not isinstance(evidence_source.get("rationale"), str)):
                raise ValueError("missing historical evidence source")
            old_flags = comparison.get("review_flags", {})
            if isinstance(old_flags, dict) and old_flags.get("state") == "known":
                historical_flags = old_flags.get("values")
                if not isinstance(historical_flags, list) or any(not isinstance(flag, str) for flag in historical_flags):
                    raise ValueError("invalid historical review flags")
            old_option = comparison.get("option_status", {})
            if isinstance(old_option, dict) and old_option.get("state") == "known":
                historical_option_status = old_option.get("value")
            old_fund = comparison.get("fund_reference_status", {})
            if isinstance(old_fund, dict):
                historical_fund_status = old_fund.get("historical")
        output = record["output"]
        if not isinstance(output, dict) or output.get("ticker") != ticker:
            raise ValueError("ticker mismatch")
        currency = output["currency"]
        if currency not in {"USD", "HKD", "CNY", "TWD"}:
            raise ValueError("unsupported currency")
        if evidence_states is not None:
            old_units = comparison.get('price_units', {})
            for key, state in evidence_states.items():
                entry = comparison[key]
                data = entry.get('data') if state == 'present' else None
                actual = output.get(key)
                unavailable = isinstance(actual, dict) and actual.get('status') == 'unavailable'
                old_conflict = isinstance(data, dict) and data.get('status') == 'unavailable' and bool(data.get('anchors'))
                if not isinstance(data, dict) or not (actual is None or unavailable or old_conflict):
                    continue
                old_anchors = data.get('anchors', {})
                if not isinstance(old_anchors, dict) or any(tier not in ('L', 'M', 'H') or not _number(value)
                                                            for tier, value in old_anchors.items()):
                    raise ValueError('invalid historical reference anchors')
                old_label = data.get('label')
                if old_label is not None and not isinstance(old_label, str):
                    raise ValueError('invalid historical reference label')
                unit_record = old_units.get(key, {}) if isinstance(old_units, dict) else {}
                unit = unit_record.get('historical') if isinstance(unit_record, dict) else None
                if unit is not None and not isinstance(unit, str) and not (
                        isinstance(unit, list) and all(isinstance(value, str) for value in unit)):
                    raise ValueError('invalid historical reference unit')
                reference_bands.append({'kind': key, 'label': old_label, 'anchors': dict(old_anchors),
                                        'unit': unit, 'status': data.get('status'), 'source': entry.get('source')})
            for key, state in evidence_states.items():
                if state == "unknown" and key + "_unknown" not in evidence_flags:
                    evidence_flags.append(key + "_unknown")
                if ((state == "present" and output.get(key) is None)
                        or (state == "absent" and output.get(key) is not None)):
                    if not any(item["field"] == key + ".presence" for item in differences):
                        differences.append({"field": key + ".presence", "kind": "band_presence_difference",
                                            "historical": state,
                                            "current": "present" if output.get(key) is not None else "absent"})
                    if "historical_values_differ" not in evidence_flags:
                        evidence_flags.append("historical_values_differ")
        if differences and "historical_values_differ" not in evidence_flags:
            evidence_flags.append("historical_values_differ")
        permitted = {f"{currency}_PER_SHARE", f"{currency}_PER_UNIT", f"{currency}_PER_FUND_UNIT"}
        price_units: list[str] = []
        units = output.get("units")
        if units is not None:
            if not isinstance(units, dict) or units.get("currency", currency) != currency:
                raise ValueError("invalid top-level units")
            price_units.extend(units[k] for k in ("price", "anchors", "candidate", "option") if k in units)
            if any(not isinstance(unit, str) or unit not in permitted for unit in price_units):
                raise ValueError("invalid price unit")
        anchors = _band(output["anchors"], permitted, price_units)
        candidate = output["candidate"]
        candidate_band = label = candidate_status = None
        if candidate is not None:
            if not isinstance(candidate, dict):
                raise ValueError("invalid candidate")
            candidate_status = candidate.get("status")
            if candidate_status is not None and (not isinstance(candidate_status, str) or not candidate_status.strip()):
                raise ValueError("invalid candidate status")
            label = candidate.get("label")
            if label is not None and (not isinstance(label, str) or not label.strip()):
                raise ValueError("invalid candidate label")
            _units(candidate, permitted, price_units)
            if candidate_status == "unavailable":
                if candidate.get("anchors") is not None:
                    raise ValueError("unavailable candidate cannot contain anchors")
            else:
                if label is None:
                    raise ValueError("missing candidate label")
                candidate_band = _band(candidate["anchors"], permitted, price_units)
        option = output["option"]
        option_band = option_value = option_status = option_label = None
        if option is not None:
            if not isinstance(option, dict):
                raise ValueError("invalid option")
            option_label = option.get('label')
            if option_label is not None and (not isinstance(option_label, str) or not option_label.strip()):
                raise ValueError('invalid option label')
            _units(option, permitted, price_units)
            option_status = option.get("status")
            if option_status is not None and (not isinstance(option_status, str) or not option_status.strip()):
                raise ValueError("invalid option status")
            if option_status == "unavailable":
                if option.get("anchors") is not None:
                    raise ValueError("unavailable option cannot contain anchors")
            else:
                option_band = _band(option["anchors"], permitted, price_units)
            value = option.get("value")
            if value is not None and (not _number(value) or value < 0):
                raise ValueError("invalid option value")
            value_unit = option.get("value_unit")
            if option_band is not None and isinstance(value_unit, str) and value_unit in permitted and value_unit in price_units:
                option_value = value
            if option_value == 0:
                option_band = None
        if len(set(price_units)) > 1:
            raise ValueError("price units differ")
        flags = output.get("review_flags", [])
        if not isinstance(flags, list) or any(not isinstance(flag, str) or not flag.strip() for flag in flags):
            raise ValueError("invalid review flags")
        flags = list(flags)
        fund_status = output.get("fund_reference_status")
        if fund_status is not None and (not isinstance(fund_status, str) or not fund_status.strip()):
            raise ValueError("invalid fund reference status")
        baskets = output.get("baskets")
        basket_error = baskets is not None and (not isinstance(baskets, dict) or any(
                not isinstance(key, str) or not key or type(value) is not int or value not in range(1, 8)
                for key, value in baskets.items()))
        if basket_error:
            baskets = None
        axis_metadata = output.get('axis_metadata')
        if axis_metadata is not None:
            if not isinstance(axis_metadata, dict) or any(not isinstance(value, dict) for value in axis_metadata.values()):
                axis_metadata = None
            else:
                try:
                    json.dumps(axis_metadata, allow_nan=False)
                except (TypeError, ValueError):
                    axis_metadata = None
                declared = output.get('source_versions', {})
                if (not axis_metadata and isinstance(declared, dict)
                        and any(name.startswith('axis:') for name in declared)):
                    axis_metadata = None
        consensus, pressure, delta, cap, rate_error, rate_format = _rate_sensitivity(output, currency, permitted, price_units)
        return status, ResultView(ticker, currency, anchors, candidate_band, label, candidate_status,
                                  option_band, option_value, option_status, tuple(flags), tuple(evidence_flags), evidence_states,
                                  tuple(differences), evidence_source, tuple(historical_flags), historical_option_status,
                                  historical_fund_status, fund_status,
                                  consensus, pressure, delta, cap, rate_error, rate_format, baskets, basket_error, ref,
                                  tuple(reference_bands), option_label, axis_metadata, reference_only, reference_double_counting,
                                  _valuation_date(output.get('valuation_as_of')))
    except (KeyError, TypeError, ValueError):
        return "invalid", None
