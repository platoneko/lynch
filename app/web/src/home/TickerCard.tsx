import type { AxisInfo, TickerRow, ValuationStatus, ZoneId } from '../types';
import { AxisLine } from './AxisLine';
import { EXTRA_CAPTION, extraBands, isHistoricalReferenceOnly, lynchClasses, rowZone, type ExtraBand } from './listModel';
import { barScale, fmt, PriceBar, priceDecimals, zoneRange, type Anchors } from './PriceBar';

const SESSION_CN = { pre: '盘前', post: '盘后', overnight: '夜盘' } as const;
const VALUATION_MESSAGE: Partial<Record<ValuationStatus, string>> = {
  missing: '暂无估值结果', unconfirmed: '估值结果待确认', stale: '估值依据已变化',
  invalid: '估值结果无效', changed: '估值结果正在更新', proof_unavailable: '估值结果暂不可验证',
  unavailable: '估值结果暂不可读取',
};
const HISTORICAL_EVIDENCE_MESSAGE: Record<string, string> = {
  confirmed_snapshot_unverified: '显示已确认的历史快照，当前执行证明不可用',
  valuation_source_unavailable: '估值来源暂不可核验，当前展示历史快照',
  valuation_source_changed: '估值来源已变化，当前展示历史快照',
  valuation_source_previously_unavailable: '接纳历史快照时，估值来源曾不可核验',
  valuation_source_previously_changed: '接纳历史快照时，估值来源曾发生变化',
  valuation_source_previously_ambiguous: '接纳时的来源检查不能区分变化和读取失败',
  external_history_source_unavailable: '外部历史数据来源暂不可读取',
  external_history_source_changed: '外部历史数据来源已变化，旧结果待复算',
  valuation_runtime_unavailable: '计算环境暂不可用',
  valuation_runtime_changed: '计算环境已变化',
  peer_result_unverified: '引用的其他标的当前估值尚未核对',
  historical_script_missing: '旧脚本版本尚未关联，历史结果待核',
  historical_recorded_source_changed: '历史快照记录的来源已变化，待核对',
  historical_candidate_upgrade_pending: '候选升级条件已触发，仍待复核',
  historical_low_display_difference: '旧文保守档与脚本显示值有差异，详见文档',
  historical_option_cost_basis_difference: '额外情景已扣首飞前费用，旧日报口径不同',
  historical_option_mid_high_two_decimals: '额外情景中性、乐观档沿用旧输出的两位小数',
  historical_conditional_funding: '历史估值有条件采用，资金投入和来源仍待核',
  historical_mid_forecast_weakened: '旧中性盈利预测已被财报削弱，待重新确定',
  historical_base_two_decimals: '基础价带沿用旧输出的两位小数',
  historical_option_mid_high_one_decimal: '额外情景中性、乐观档沿用旧输出的一位小数',
  historical_separate_syntax_error: '另一份旧日志记录语法错误，是否同次运行待核',
  historical_earlier_failed_log: '另有较早旧日志记录核对失败，与本次记录的关系待核',
  historical_run_log_incomplete: '历史运行日志不完整或有冲突，待核',
  historical_financial_basis_unknown: '历史估值的财务基准日期待核',
  historical_base_four_decimals: '基础三档沿用旧脚本输出的四位小数',
  historical_option_all_two_decimals: '额外情景沿用旧脚本输出的两位小数',
  historical_candidate_reference_conflict: '旧候选与现有独立对照口径不同，待核',
  current_framework_funding_gap_unquantified: '当前框架提示扩产资金缺口未量化，请结合文档判断',
  execution_unverified: '计算来源尚未独立核验',
  historical_material_unreviewed: '历史资料待核',
  historical_source_changed: '历史资料在接纳期间发生变化，尚需核对',
  historical_transcription_attested: '历史数值由引用材料人工整理，尚需复核',
  precision_source_conflict: '历史精度说明存在冲突，尚需核对',
  candidate_unknown: '旧材料的候选价带是否完整仍待核对',
  option_unknown: '旧材料的新业务价带是否完整仍待核对',
  anchors_unknown: '旧材料的基础估值档位尚未完整核对',
  currency_unknown: '旧材料的计价币种未明确，尚需核对',
  currency_attested: '历史价带币种由引用资料人工整理，尚需复核',
  currency_citation_unverified: '引用的币种说明尚不能确认估值价带的币种',
  historical_identity_unknown: '旧材料的标的身份尚需核对',
  candidate_anchors_unknown: '旧材料的候选价带档位尚未完整核对',
  candidate_label_unknown: '旧材料的候选名称尚需核对',
  historical_option_internally_inconsistent: '旧材料自身口径待核：新业务同时标不可计算并列有价格',
  historical_candidate_internally_inconsistent: '旧材料自身口径待核：候选同时标不可计算并列有价格',
  candidate_absence_attested: '候选价带的缺省由历史材料人工核对',
  option_absence_attested: '新业务价带的缺省由历史材料人工核对',
  option_anchors_unknown: '旧材料的新业务价带档位仍待核对',
  anchors_unit_unknown: '历史估值区间的计价单位待核对',
  candidate_unit_unknown: '历史候选价带的计价单位待核对',
  option_unit_unknown: '历史新业务价带的计价单位待核对',
  option_value_unit_unknown: '历史新业务价值的单位待核对',
  option_value_unknown: '历史新业务价值的数额仍待核对',
  option_value_unit_incompatible: '历史新业务价值与价带的计价口径不同',
  review_flags_unknown: '旧框架的风险提示是否完整仍待核对',
  option_status_unknown: '旧材料的新业务状态仍待核对',
  display_precision_attested: '历史价格按原资料的显示精度对照',
  price_unit_attested: '历史价格单位按资料说明对照',
  precision_unknown: '历史价格的精度未标明，数值一致性待核',
  fund_reference_status_unknown: '旧材料的基金参考状态仍待核对',
};

const EVIDENCE_FIELD: Record<string, string> = {
  'anchors.L': '保守估值', 'anchors.M': '中性估值', 'anchors.H': '乐观估值',
  'candidate.anchors.L': '候选保守估值', 'candidate.anchors.M': '候选中性估值', 'candidate.anchors.H': '候选乐观估值',
  'option.anchors.L': '新业务保守估值', 'option.anchors.M': '新业务中性估值', 'option.anchors.H': '新业务乐观估值',
  'candidate.presence': '候选价带状态', 'option.presence': '新业务价带状态', 'candidate.label': '候选名称',
  'option.value': '新业务价值', 'option.value_unit': '新业务价值单位', 'option.status': '新业务状态',
  'anchors.unit': '基础价带单位', 'candidate.unit': '候选价带单位', 'option.unit': '新业务价带单位',
  'anchors.unit_source': '基础价带历史单位', 'candidate.unit_source': '候选价带历史单位', 'option.unit_source': '新业务价带历史单位',
  'currency': '计价货币', 'currency.metadata': '历史计价货币', 'review_flags': '风险提示',
  'currency.source': '历史币种来源',
  'fund_reference_status': '基金参考状态',
  'ticker': '历史材料中的标的',
  'option.source_consistency': '历史新业务价带口径',
  'candidate.source_consistency': '历史候选价带口径',
};

function evidenceUnit(value: string | string[] | null | undefined): string {
  if (Array.isArray(value)) return value.map(evidenceUnit).join('、');
  if (!value) return '单位未注明';
  const currencies: Record<string, string> = { USD: '美元', CNY: '人民币', HKD: '港元', EUR: '欧元', TWD: '新台币' };
  const match = /^([A-Z]{3})_PER_(?:(\d+)_)?(SHARE|UNIT|FUND_UNIT)$/.exec(value);
  if (match) return `${currencies[match[1]] ?? match[1]}/${match[2] ?? ''}${match[3] === 'SHARE' ? '股' : '份'}`;
  const total = /^([A-Z]{3})_MILLION$/.exec(value);
  if (total) return `百万${currencies[total[1]] ?? total[1]}`;
  return value;
}

function evidenceFieldIsUnit(field: string): boolean {
  return field.endsWith('.unit') || field.endsWith('.unit_source') || field === 'option.value_unit';
}

function evidenceValue(value: unknown, field: string): string {
  if (value == null) return '未提供';
  if (field.startsWith('currency') && typeof value === 'string') {
    const names: Record<string, string> = {USD: '美元', HKD: '港元', CNY: '人民币', TWD: '新台币', EUR: '欧元'};
    return names[value] ?? value;
  }
  if (evidenceFieldIsUnit(field)
      && (typeof value === 'string' || (Array.isArray(value) && value.every((item) => typeof item === 'string')))) {
    return evidenceUnit(value as string | string[]);
  }
  if (Array.isArray(value)) return `${value.length} 项`;
  if (typeof value === 'string') {
    if (value === 'present') return '存在';
    if (value === 'absent') return '不存在';
    if (value === 'requires_reconciliation') return '需要核对';
    if (value === 'conditional') return '条件情景';
    return value;
  }
  if (typeof value === 'number') return String(value);
  return '有记录，详见历史材料';
}

export function TickerCard({ row, label, axes, onOpen, onOpenAxis }: {
  row: TickerRow;
  label: (id: string) => string; // zone and Lynch class ids → Chinese labels
  axes: ReadonlyMap<string, AxisInfo>;
  onOpen: (ticker: string) => void;
  onOpenAxis: (name: string) => void;
}) {
  const open = () => onOpen(row.ticker);
  // A div, not a button: the axis links inside it are buttons, which a button may not contain.
  return (
    <div className="card" role="button" tabIndex={0} onClick={open}
      onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); open(); } }}>
      <div className="card-line1">
        <span className="card-ticker">{row.ticker}</span>
        <span className="card-name">{row.name ?? ''}</span>
        {row.archived && <span className="tag tag-archived">已归档</span>}
        {row.valuation_status === 'legacy_accepted' && <span className="tag tag-muted">历史框架沿用，计算尚待复核</span>}
        {row.valuation_status === 'confirmed_snapshot' && <span className="tag tag-muted">已确认快照，当前执行证明不可用</span>}
        {row.band && (
          <span className="card-price">
            {row.band.session && <span className="card-session">{SESSION_CN[row.band.session]}</span>}
            {fmt(row.band.price, priceDecimals(row.ticker))} {row.currency}
          </span>
        )}
      </div>
      {row.error ? (
        <div className="card-error">解析失败：{row.error}</div>
      ) : (
        <>
          <div className="card-classes">{lynchClasses(row, label)}</div>
          <AxisLine row={row} axes={axes} onOpenAxis={onOpenAxis} />
          <ReviewCautions row={row} />
          <CardZone row={row} label={label} />
        </>
      )}
    </div>
  );
}

/** Status codes remain available to research tools; readers get a plain-language pointer to the document. */
export function ReviewCautions({ row }: { row: TickerRow }) {
  const hasValuation = row.L != null && row.M != null && row.H != null;
  const referenceOnly = isHistoricalReferenceOnly(row);
  const basisLabel = referenceOnly ? '历史估值基准' : '估值基准';
  const hasHistoricalDoubleCounting = !!row.historical_reference_double_counting || [row.review_flags, row.historical_review_flags]
    .some((flags) => flags?.includes('historical_double_counting'));
  const optionStatuses = [row.option_review_status ?? row.option?.review_status, row.historical_option_status];
  const needsOptionReview = optionStatuses.some((status) => status != null && status !== 'conditional');
  const needsCandidateReview = row.candidate_review_status != null && row.candidate_review_status !== 'conditional';
  const fundStatuses = [row.fund_reference_status, row.historical_fund_reference_status];
  const needsFundReview = fundStatuses.some((status) => status != null && status !== 'verified');
  const differences = row.historical_evidence_differences ?? [];
  const references = row.historical_reference_bands ?? [];
  const priority: Record<string, number> = {external_history_source_unavailable: 0,
    external_history_source_changed: 1, valuation_runtime_unavailable: 2};
  const evidenceFlags = [...(row.historical_evidence_flags ?? [])].sort((a, b) =>
    (priority[a] ?? 3) - (priority[b] ?? 3));
  if (!hasValuation && !referenceOnly && !(row.review_flags?.length || row.historical_review_flags?.length) && !needsOptionReview && !needsCandidateReview && !needsFundReview
      && !row.historical_evidence_flags?.length && !differences.length && !references.length) return null;
  return <>
    {hasValuation && <div className="tag tag-muted">{row.valuation_as_of ? `${basisLabel}：${row.valuation_as_of}` : `${basisLabel}日期待核`}</div>}
    {evidenceFlags.filter((flag) => flag !== 'historical_values_differ' || !differences.length).map((flag) => (
      <div className="tag tag-muted" key={flag}>{flag === 'precision_unknown' && (evidenceFlags.includes('historical_option_mid_high_two_decimals') || evidenceFlags.includes('historical_base_two_decimals') || evidenceFlags.includes('historical_option_mid_high_one_decimal'))
        ? '其他历史数字的精度仍待核' : (HISTORICAL_EVIDENCE_MESSAGE[flag] ?? '历史材料部分细节待核对')}</div>
    ))}
    {differences.map((difference, index) => (
      <div className="tag tag-muted" key={`${difference.field}-${index}`}>
        {difference.kind === 'precision_unknown_difference' ? '精度未知，是否一致待核' :
          difference.kind === 'historical_internal_conflict' ? '旧材料自身口径待核' : '与历史值不一致，待核'}：
        {EVIDENCE_FIELD[difference.field] ?? '历史资料'} · 历史值 {evidenceValue(difference.historical, difference.field)}
        {difference.historical_unit != null && !evidenceFieldIsUnit(difference.field) && ` ${evidenceUnit(difference.historical_unit)}`} · 脚本值 {evidenceValue(difference.current, difference.field)}
        {difference.current_unit != null && !evidenceFieldIsUnit(difference.field) && ` ${evidenceUnit(difference.current_unit)}`}
        {row.historical_evidence_source && ` · 来源 ${row.historical_evidence_source.rationale}（${row.historical_evidence_source.reference}）`}
      </div>
    ))}
    {references.map((reference, index) => {
      const tiers = (['L', 'M', 'H'] as const).map((tier) => reference.anchors[tier] == null ? '未注明' : String(reference.anchors[tier]));
      return <div className="tag tag-muted" key={`${reference.kind}-${index}`}>
        历史{reference.kind === 'candidate' ? '候选' : '新业务'}价带{reference.label ? `（${reference.label}）` : ''}：
        {tiers.join(' / ')} {evidenceUnit(reference.unit)} · 仅供对照，不是脚本正式价带
        {reference.source === 'operator_attested' && ' · 人工整理，待核'}
        {row.historical_evidence_source && ` · 来源 ${row.historical_evidence_source.rationale}（${row.historical_evidence_source.reference}）`}
      </div>;
    })}
    {hasHistoricalDoubleCounting && <div className="tag tag-muted">旧估值存在重复计价，仅供历史对照</div>}
    {isHistoricalReferenceOnly(row) && !hasHistoricalDoubleCounting &&
      <div className="tag tag-muted">旧估值仅供历史对照，不作为当前价带</div>}
    {!!(row.review_flags?.length || row.historical_review_flags?.length) && <div className="tag tag-muted">估值假设有待核对事项，详见正文</div>}
    {needsCandidateReview && <div className="tag tag-muted">候选价带暂不可计算或需要核对</div>}
    {needsOptionReview && <div className="tag tag-muted">{row.option_review_status === 'unavailable' ? '新业务价带暂不可计算' : '新业务估值需要核对，详见正文'}</div>}
    {needsFundReview && <div className="tag tag-muted">基金参考价格或日期尚需核对</div>}
  </>;
}

function CardZone({ row, label }: { row: TickerRow; label: (id: string) => string }) {
  const zone = rowZone(row);
  if (!row.band || !zone) {
    const historical = isHistoricalReferenceOnly(row);
    const base = row.L != null && row.M != null && row.H != null ? { L: row.L, M: row.M, H: row.H } : null;
    const dp = priceDecimals(row.ticker);
    const prices = (a: Anchors) => `${fmt(a.L, dp)} / ${fmt(a.M, dp)} / ${fmt(a.H, dp)}`;
    const resultMessage = row.valuation_status ? VALUATION_MESSAGE[row.valuation_status] : undefined;
    return <>
      {resultMessage && <div className="tag tag-muted">{resultMessage}</div>}
      {(row.quote_error || (!resultMessage && !historical)) &&
        <div className="tag tag-muted">{row.quote_error ?? (base ? '估值档位不可用' : '估值结果不可用')}</div>}
      {base && <div className="band-head">{historical ? '历史估值（仅供对照）' : '估值区间'}：{prices(base)} {row.currency}</div>}
      {base && extraBands(row).map((extra) => (
        <div key={extra.kind} className="band-head">{historical ? '历史' : ''}{extra.label}：{prices(extra.anchors)} {row.currency}</div>
      ))}
    </>;
  }
  const { band } = row;
  const dp = priceDecimals(row.ticker);
  const base = { L: row.L!, M: row.M!, H: row.H! };
  const extras = extraBands(row);
  // All bars share one scale so each extra band's shift against the base is visible.
  const scale = barScale([base, ...extras.map((e) => e.anchors)]);
  return (
    <>
      <div className="band">
        <div className="band-head">
          {extras.length > 0 && <span className="band-caption">底牌</span>}
          <ZoneTag zone={zone} anchors={base} label={label} dp={dp} />
        </div>
        <PriceBar price={band.price} anchors={base} scale={scale} zone={zone} dp={dp} />
      </div>
      {extras.map((e) => (
        <div key={e.kind} className="band">
          <BandHead band={e} zoneLabel={label} showRange dp={dp} />
          <PriceBar price={band.price} anchors={e.anchors} scale={scale} zone={e.zone} dp={dp} />
        </div>
      ))}
    </>
  );
}

/** The zone tag followed, on the card, by the price range of that zone on this band. */
function ZoneTag({ zone, anchors, label, showRange = true, dp }: {
  zone: ZoneId;
  anchors: Anchors;
  label: (id: string) => string;
  showRange?: boolean;
  dp?: number;
}) {
  return (
    <>
      <span className={`tag zone-bg-${zone}`}>{label(zone)}</span>
      {showRange && <span className="zone-range">{zoneRange(zone, anchors, dp)}</span>}
    </>
  );
}

/** 「+V [zone] +V 18.17」 or 「候选 [zone] label」, shared by the card and the doc page header. */
export function BandHead({ band, zoneLabel, showRange = false, dp, historical = false }: {
  band: ExtraBand;
  zoneLabel: (id: string) => string;
  showRange?: boolean; // the card shows the zone's price range; the doc page lists the prices elsewhere
  dp?: number;
  historical?: boolean;
}) {
  const { zone, label } = band;
  return (
    <div className="band-head">
      <span className="band-caption">{historical ? '历史' : ''}{EXTRA_CAPTION[band.kind]}</span>
      {/* Zones come only from the server; a missing one is shown as such, never computed here. */}
      {zone
        ? <ZoneTag zone={zone} anchors={band.anchors} label={zoneLabel} showRange={showRange} dp={dp} />
        : <span className="tag tag-muted">—</span>}
      <span className="band-label">{label}</span>
    </div>
  );
}
