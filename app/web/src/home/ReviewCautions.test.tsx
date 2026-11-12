import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { TickerRow } from '../types';
import { TickerDocHead } from '../doc/DocPage';
import { TickerCard } from './TickerCard';

const row: TickerRow = {
  ticker: 'AAA.US', name: 'Alpha', market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
  currency: 'USD', L: 10, M: 20, H: 30,
  option: { L: 10, M: 22, H: 34, review_status: 'requires_reconciliation' }, candidate: null,
  axes: [], ref_axes: [], consensus: null, band: null, quote_error: null, error: null, archived: false,
  framework_updated: null, last_earnings: null, next_earnings: null, stale_after_earnings: false,
  valuation_status: 'legacy_accepted', review_flags: ['single_broker_path_not_above_high_consensus'],
  historical_evidence_flags: ['candidate_unknown', 'anchors_unit_unknown'],
};

describe('review cautions', () => {
  it('keeps existing notices while showing saved-log conflicts and known precision', () => {
    const historical = {...row, historical_evidence_flags: ['historical_conditional_funding',
      'historical_mid_forecast_weakened', 'historical_base_two_decimals',
      'historical_financial_basis_unknown', 'historical_base_four_decimals',
      'historical_option_mid_high_one_decimal', 'historical_separate_syntax_error',
      'historical_earlier_failed_log', 'historical_run_log_incomplete', 'precision_unknown']} as TickerRow;
    const card = renderToStaticMarkup(<TickerCard row={historical} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    const doc = renderToStaticMarkup(<TickerDocHead ticker="AAA.US" row={historical}
      targets={{pool: new Set(), axes: new Set()}} axes={new Map()} zoneLabel={(id) => id} onOpenAxis={() => {}} />);
    for (const html of [card, doc]) {
      expect(html).toContain('历史估值有条件采用，资金投入和来源仍待核');
      expect(html).toContain('旧中性盈利预测已被财报削弱，待重新确定');
      expect(html).toContain('基础价带沿用旧输出的两位小数');
      expect(html).toContain('历史估值的财务基准日期待核');
      expect(html).toContain('额外情景中性、乐观档沿用旧输出的一位小数');
      expect(html).toContain('另一份旧日志记录语法错误，是否同次运行待核');
      expect(html).toContain('另有较早旧日志记录核对失败，与本次记录的关系待核');
      expect(html).toContain('历史运行日志不完整或有冲突，待核');
      expect(html).toContain('其他历史数字的精度仍待核');
    }
  });

  it('shows the known historical base precision and unknown financial date', () => {
    const withNotes = {...row, historical_evidence_flags: ['historical_financial_basis_unknown',
      'historical_base_four_decimals', 'historical_candidate_reference_conflict',
      'current_framework_funding_gap_unquantified']} as TickerRow;
    const html = renderToStaticMarkup(<TickerCard row={withNotes} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('历史估值的财务基准日期待核');
    expect(html).toContain('基础三档沿用旧脚本输出的四位小数');
    expect(html).toContain('旧候选与现有独立对照口径不同，待核');
    expect(html).toContain('当前框架提示扩产资金缺口未量化，请结合文档判断');
  });

  it('explains historical funding and weakened forecasts without hiding the band', () => {
    const historical = {...row, historical_evidence_flags: ['historical_conditional_funding',
      'historical_mid_forecast_weakened', 'historical_base_two_decimals', 'precision_unknown']} as TickerRow;
    const card = renderToStaticMarkup(<TickerCard row={historical} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    const doc = renderToStaticMarkup(<TickerDocHead ticker="AAA.US" row={historical}
      targets={{pool: new Set(), axes: new Set()}} axes={new Map()} zoneLabel={(id) => id} onOpenAxis={() => {}} />);
    for (const html of [card, doc]) {
      expect(html).toContain('历史估值有条件采用，资金投入和来源仍待核');
      expect(html).toContain('旧中性盈利预测已被财报削弱，待重新确定');
      expect(html).toContain('基础价带沿用旧输出的两位小数');
      expect(html).toContain('其他历史数字的精度仍待核');
    }
    expect(card).toContain('估值区间');
  });

  it('shows the known precision of the historical option only where it applies', () => {
    const withPrecision = {...row, historical_evidence_flags: ['historical_option_mid_high_two_decimals', 'precision_unknown']} as TickerRow;
    const ordinary = {...row, historical_evidence_flags: ['precision_unknown']} as TickerRow;
    const renderCard = (item: TickerRow) => renderToStaticMarkup(<TickerCard row={item} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    const document = renderToStaticMarkup(<TickerDocHead ticker="AAA.US" row={withPrecision}
      targets={{pool: new Set(), axes: new Set()}} axes={new Map()} zoneLabel={(id) => id} onOpenAxis={() => {}} />);
    expect(renderCard(withPrecision)).toContain('额外情景中性、乐观档沿用旧输出的两位小数');
    expect(document).toContain('额外情景中性、乐观档沿用旧输出的两位小数');
    expect(renderCard(withPrecision)).toContain('其他历史数字的精度仍待核');
    expect(renderCard(withPrecision)).not.toContain('历史价格的精度未标明');
    expect(renderCard(ordinary)).not.toContain('额外情景中性、乐观档沿用旧输出的两位小数');
    expect(renderCard(ordinary)).toContain('历史价格的精度未标明');
  });

  it('identifies known double-counting as a historical comparison on both views', () => {
    const historical = {...row, review_flags: ['historical_double_counting']} as TickerRow;
    const card = renderToStaticMarkup(<TickerCard row={historical} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    const doc = renderToStaticMarkup(<TickerDocHead ticker="AAA.US" row={historical}
      targets={{pool: new Set(), axes: new Set()}} axes={new Map()} zoneLabel={(id) => id} onOpenAxis={() => {}} />);
    for (const html of [card, doc]) {
      expect(html).toContain('旧估值存在重复计价，仅供历史对照');
      expect(html).toContain('估值区间');
    }
  });

  it('shows understandable warnings on the home card without hiding the band', () => {
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('估值假设有待核对事项');
    expect(html).toContain('新业务估值需要核对');
    expect(html).toContain('历史框架沿用，计算尚待复核');
    expect(html).toContain('旧材料的候选价带是否完整仍待核对');
    expect(html).toContain('历史估值区间的计价单位待核对');
    expect(html).toContain('估值区间');
    expect(html).not.toContain('single_broker_path_not_above_high_consensus');
  });

  it('shows the same cautions next to the document valuation', () => {
    const html = renderToStaticMarkup(<TickerDocHead ticker="AAA.US" row={row}
      targets={{ pool: new Set(), axes: new Set() }} axes={new Map()} zoneLabel={(id) => id}
      onOpenAxis={() => {}} />);
    expect(html).toContain('估值假设有待核对事项');
    expect(html).toContain('新业务估值需要核对');
    expect(html).toContain('历史框架沿用，计算尚待复核');
    expect(html).toContain('旧材料的候选价带是否完整仍待核对');
    expect(html).toContain('历史估值区间的计价单位待核对');
  });

  it('shows historical and script values, units, source and fund reconciliation on both views', () => {
    const withDifference = {
      ...row, M: 200, H: 300, fund_reference_status: 'requires_reconciliation',
      historical_evidence_differences: [{field: 'anchors.M', kind: 'unrounded_difference',
        historical: 20, current: 200, historical_unit: 'USD_PER_SHARE', current_unit: 'USD_PER_SHARE'}],
      historical_evidence_source: {reference: 'old-result.json', rationale: '2025 年旧估值材料'},
    } as TickerRow;
    const card = renderToStaticMarkup(<TickerCard row={withDifference} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    const doc = renderToStaticMarkup(<TickerDocHead ticker="AAA.US" row={withDifference}
      targets={{ pool: new Set(), axes: new Set() }} axes={new Map()} zoneLabel={(id) => id} onOpenAxis={() => {}} />);
    for (const html of [card, doc]) {
      expect(html).toContain('与历史值不一致，待核');
      expect(html).toContain('历史值 20');
      expect(html).toContain('脚本值 200');
      expect(html).toContain('美元/股');
      expect(html).toContain('2025 年旧估值材料');
      expect(html).toContain('基金参考价格或日期尚需核对');
    }
  });

  it('keeps unavailable extra bands visible as statuses without inventing prices', () => {
    const unavailable = {...row, candidate: null, option: null,
      candidate_review_status: 'unavailable', option_review_status: 'unavailable'} as TickerRow;
    const html = renderToStaticMarkup(<TickerCard row={unavailable} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('候选价带暂不可计算或需要核对');
    expect(html).toContain('新业务价带暂不可计算');
    expect(html).toContain('估值区间');
  });

  it('shows historical extra bands as references only, with source and units', () => {
    const reference = {...row, candidate: null, option: null,
      historical_reference_bands: [{kind: 'candidate', label: '扩产',
        anchors: {L: 11, M: 22, H: 33}, unit: 'USD_PER_SHARE'}],
      historical_evidence_source: {reference: 'old-result.json', rationale: '旧估值材料'}} as TickerRow;
    const card = renderToStaticMarkup(<TickerCard row={reference} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    const doc = renderToStaticMarkup(<TickerDocHead ticker="AAA.US" row={reference}
      targets={{ pool: new Set(), axes: new Set() }} axes={new Map()} zoneLabel={(id) => id} onOpenAxis={() => {}} />);
    for (const html of [card, doc]) {
      expect(html).toContain('历史候选价带');
      expect(html).toContain('扩产');
      expect(html).toContain('11 / 22 / 33');
      expect(html).toContain('美元/股');
      expect(html).toContain('旧估值材料');
      expect(html).toContain('不是脚本正式价带');
    }
  });

  it('labels an operator-transcribed historical band as still needing review', () => {
    const reference = {...row, candidate: null, option: null,
      historical_reference_bands: [{kind: 'candidate', label: '扩产', source: 'operator_attested',
        anchors: {L: 11, M: 22, H: 33}, unit: 'USD_PER_SHARE'}]} as TickerRow;
    const html = renderToStaticMarkup(<TickerCard row={reference} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('人工整理，待核');
    expect(html).toContain('不是脚本正式价带');
  });

  it('does not assert inconsistency when source precision is unknown', () => {
    const uncertain = {...row, historical_evidence_flags: ['precision_unknown', 'historical_values_differ'],
      historical_evidence_differences: [{field: 'anchors.M', kind: 'precision_unknown_difference',
        historical: 20, current: 20.001, historical_unit: 'USD_PER_SHARE', current_unit: 'USD_PER_SHARE'}],
      historical_evidence_source: {reference: 'old.json', rationale: '旧材料'}} as TickerRow;
    const html = renderToStaticMarkup(<TickerCard row={uncertain} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('精度未知，是否一致待核');
    expect(html).not.toContain('与历史值不一致，待核');
    expect(html).toContain('历史值 20');
    expect(html).toContain('脚本值 20.001');
  });

  it('exposes old unavailable status and prices as an internal source conflict', () => {
    const contradictory = {...row, option: null, option_review_status: 'unavailable',
      historical_evidence_flags: ['historical_option_internally_inconsistent'],
      historical_evidence_differences: [{field: 'option.source_consistency', kind: 'historical_internal_conflict',
        historical: {status: 'unavailable', anchors: {L: 11, M: 22, H: 33}}, current: 'unavailable'}],
      historical_reference_bands: [{kind: 'option', label: null, status: 'unavailable',
        anchors: {L: 11, M: 22, H: 33}, unit: 'USD_PER_SHARE'}],
      historical_evidence_source: {reference: 'old.json', rationale: '旧材料'}} as TickerRow;
    const html = renderToStaticMarkup(<TickerCard row={contradictory} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('旧材料自身口径待核');
    expect(html).toContain('11 / 22 / 33');
    expect(html).toContain('不是脚本正式价带');
  });

  it('shows old unavailable candidate prices as an internal source conflict on both views', () => {
    const contradictory = {...row, candidate: null, candidate_review_status: 'unavailable',
      historical_evidence_flags: ['historical_candidate_internally_inconsistent'],
      historical_evidence_differences: [{field: 'candidate.source_consistency', kind: 'historical_internal_conflict',
        historical: {status: 'unavailable', anchors: {L: 11, M: 22, H: 33}}, current: 'unavailable'}],
      historical_reference_bands: [{kind: 'candidate', label: '扩产', status: 'unavailable',
        anchors: {L: 11, M: 22, H: 33}, unit: 'USD_PER_SHARE'}],
      historical_evidence_source: {reference: 'old.json', rationale: '旧材料'}} as TickerRow;
    const card = renderToStaticMarkup(<TickerCard row={contradictory} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    const doc = renderToStaticMarkup(<TickerDocHead ticker="AAA.US" row={contradictory}
      targets={{pool: new Set(), axes: new Set()}} axes={new Map()} zoneLabel={(id) => id} onOpenAxis={() => {}} />);
    for (const html of [card, doc]) {
      expect(html).toContain('候选同时标不可计算并列有价格');
      expect(html).toContain('旧材料自身口径待核');
      expect(html).toContain('扩产');
      expect(html).toContain('11 / 22 / 33');
      expect(html).toContain('不是脚本正式价带');
    }
  });

  it('prioritizes an unavailable external history source without claiming a match', () => {
    const missingSource = {...row, historical_evidence_flags: ['candidate_unknown', 'valuation_runtime_unavailable',
      'external_history_source_changed', 'external_history_source_unavailable']} as TickerRow;
    const html = renderToStaticMarkup(<TickerCard row={missingSource} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('外部历史数据来源暂不可读取');
    expect(html).toContain('外部历史数据来源已变化，旧结果待复算');
    expect(html).toContain('计算环境暂不可用');
    expect(html.indexOf('外部历史数据来源暂不可读取')).toBeLessThan(html.indexOf('旧材料的候选价带是否完整仍待核对'));
    expect(html).not.toContain('来源匹配');
  });

  it('shows frozen valuation source warnings on the home card and document', () => {
    const frozen = {...row, historical_evidence_flags: ['valuation_source_unavailable', 'valuation_source_changed']} as TickerRow;
    const card = renderToStaticMarkup(<TickerCard row={frozen} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    const doc = renderToStaticMarkup(<TickerDocHead ticker="AAA.US" row={frozen}
      targets={{pool: new Set(), axes: new Set()}} axes={new Map()} zoneLabel={(id) => id} onOpenAxis={() => {}} />);
    for (const html of [card, doc]) {
      expect(html).toContain('估值来源暂不可核验');
      expect(html).toContain('估值来源已变化，当前展示历史快照');
    }
  });

  it('distinguishes past source observations from the current status', () => {
    const frozen = {...row, historical_evidence_flags: ['valuation_source_previously_unavailable',
      'valuation_source_previously_changed']} as TickerRow;
    const card = renderToStaticMarkup(<TickerCard row={frozen} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    const doc = renderToStaticMarkup(<TickerDocHead ticker="AAA.US" row={frozen}
      targets={{pool: new Set(), axes: new Set()}} axes={new Map()} zoneLabel={(id) => id} onOpenAxis={() => {}} />);
    for (const html of [card, doc]) {
      expect(html).toContain('接纳历史快照时，估值来源曾不可核验');
      expect(html).toContain('接纳历史快照时，估值来源曾发生变化');
      expect(html).not.toContain('估值来源暂不可核验');
    }
  });
});
