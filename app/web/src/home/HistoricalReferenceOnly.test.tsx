import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { TickerRow } from '../types';
import { TickerDocHead } from '../doc/DocPage';
import { TickerCard } from './TickerCard';
import { extraBands, rowZone } from './listModel';

const row: TickerRow = {
  ticker: 'FAKE.HK', name: 'Fictional', market: 'HK', lynch_primary: 'stalwart', lynch_secondary: null,
  currency: 'HKD', L: 10, M: 20, H: 30,
  candidate: {label: 'fictional', L: 12, M: 22, H: 32}, option: {L: 10, M: 23, H: 35},
  band: {price: 15, session: null, as_of: '2040-01-01', zone: 'neutral',
    candidate_zone: 'neutral', option_zone: 'neutral'},
  axes: [], ref_axes: [], consensus: null, quote_error: null, error: null, archived: false,
  framework_updated: null, last_earnings: null, next_earnings: null, stale_after_earnings: false,
  valuation_status: 'legacy_accepted', review_flags: ['historical_double_counting'],
};

describe('historical-only valuation', () => {
  it('keeps the result basis notices in the document rather than on the home card', () => {
    for (const day of ['2026-06-30', null]) {
      const historical = {...row, historical_reference_only: true, valuation_as_of: day};
      const card = renderToStaticMarkup(<TickerCard row={historical} label={(id) => id} axes={new Map()}
        onOpen={() => {}} onOpenAxis={() => {}} />);
      const doc = renderToStaticMarkup(<TickerDocHead ticker="FAKE.HK" row={historical}
        targets={{pool: new Set(), axes: new Set()}} axes={new Map()} zoneLabel={(id) => id} onOpenAxis={() => {}} />);
      const expected = day ? `历史估值基准：${day}` : '历史估值基准日期待核';
      expect(doc).toContain(expected);
      expect(card).not.toContain('估值基准');
    }
    const current = {...row, review_flags: [], valuation_as_of: '2026-06-30'};
    const card = renderToStaticMarkup(<TickerCard row={current} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(card).not.toContain('估值基准');
    expect(card).not.toContain('历史估值基准');
    const missing = {...current, valuation_as_of: null};
    const missingCard = renderToStaticMarkup(<TickerCard row={missing} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(missingCard).not.toContain('估值基准日期待核');
  });

  it('never shows stale current zones but retains the saved interval', () => {
    expect(rowZone(row)).toBeNull();
    expect(extraBands(row).every((band) => band.zone === null)).toBe(true);
    const card = renderToStaticMarkup(<TickerCard row={row} label={(id) => `分区:${id}`} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    const doc = renderToStaticMarkup(<TickerDocHead ticker="FAKE.HK" row={row}
      targets={{pool: new Set(), axes: new Set()}} axes={new Map()} zoneLabel={(id) => `分区:${id}`} onOpenAxis={() => {}} />);
    for (const html of [card, doc]) {
      expect(html).not.toContain('分区:neutral');
    }
    expect(doc).toContain('旧估值存在重复计价，仅供历史对照');
    expect(card).not.toContain('旧估值存在重复计价，仅供历史对照');
    expect(card).toContain('估值区间');
    expect(card).toContain('10.00 / 20.00 / 30.00');
    expect(card).toContain('fictional');
    expect(card).not.toContain('估值档位不可用');
  });

  it('retains the current zone when only the old comparison carried the flag', () => {
    const corrected = {...row, review_flags: ['corrected_assumptions_require_research_review'],
      historical_review_flags: ['historical_double_counting']};
    expect(rowZone(corrected)).toBe('neutral');
    expect(extraBands(corrected).every((band) => band.zone === 'neutral')).toBe(true);
  });

  it('labels a generic historical reference without claiming double counting', () => {
    const historical = {...row, review_flags: ['historical_reference_only']};
    expect(rowZone(historical)).toBeNull();
    const card = renderToStaticMarkup(<TickerCard row={historical} label={(id) => `分区:${id}`} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(card).not.toContain('旧估值仅供历史对照，不作为当前价带');
    expect(card).toContain('估值区间');
    expect(card).not.toContain('重复计价');
    expect(card).not.toContain('分区:neutral');
  });

  it('respects current-record reference states without changing the stored review flags', () => {
    const generic = {...row, review_flags: [], historical_reference_only: true};
    const double = {...generic, historical_reference_double_counting: true};
    for (const item of [generic, double]) {
      expect(rowZone(item)).toBeNull();
      const html = renderToStaticMarkup(<TickerCard row={item} label={(id) => `分区:${id}`} axes={new Map()}
        onOpen={() => {}} onOpenAxis={() => {}} />);
      expect(html).toContain('估值区间');
      expect(html).not.toContain('分区:neutral');
      expect(html).not.toContain('旧估值存在重复计价，仅供历史对照');
      expect(html).not.toContain('旧估值仅供历史对照，不作为当前价带');
    }
  });
});
