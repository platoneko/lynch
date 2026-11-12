import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { TickerRow } from '../types';
import { TickerCard } from './TickerCard';

const pricedRow: TickerRow = {
  ticker: 'AAA.US', name: 'Alpha', market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
  currency: 'USD', L: 10, M: 20, H: 30, option: null, candidate: null, axes: [], ref_axes: [],
  consensus: null, band: {price: 18, session: null, as_of: '2026-11-12', zone: 'neutral',
    option_zone: null, candidate_zone: null}, quote_error: null, error: null, archived: false,
  framework_updated: null, last_earnings: null, next_earnings: null, stale_after_earnings: false,
};

describe('TickerCard', () => {
  it('keeps the valuation and price visible without showing internal migration notices', () => {
    const row: TickerRow = {...pricedRow, valuation_status: 'legacy_accepted',
      review_flags: ['single_broker_path_not_above_high_consensus'],
      historical_evidence_flags: ['valuation_source_unavailable', 'valuation_runtime_unavailable',
        'historical_transcription_attested', 'historical_financial_basis_unknown']};
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => `分区:${id}`} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('18.00');
    expect(html).toContain('分区:neutral');
    expect(html).toContain('15.00–25.00');
    expect(html).toContain('10.00');
    expect(html).toContain('30.00');
    expect(html).not.toContain('历史框架沿用');
    expect(html).not.toContain('来源暂不可核验');
    expect(html).not.toContain('计算环境暂不可用');
    expect(html).not.toContain('待核');
  });

  it.each([
    ['601600.SH', 'A', '中国铝业 / Aluminum Corporation of China', '中国铝业'],
    ['1378.HK', 'HK', '中国宏桥 / China Hongqiao', '中国宏桥'],
  ] as const)('prioritizes the stock name for %s while keeping the code secondary', (ticker, market, name, shortName) => {
    const row = {...pricedRow, ticker, market, name};
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain(`<span class="card-ticker">${shortName}</span>`);
    expect(html).toContain(`<span class="card-name">${ticker}</span>`);
    expect(html).not.toContain(name);
  });

  it('keeps a US stock code as its primary title', () => {
    const row = {...pricedRow, ticker: 'NVDA.US', name: '英伟达 / NVIDIA'};
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('<span class="card-ticker">NVDA.US</span>');
    expect(html).toContain('<span class="card-name">英伟达 / NVIDIA</span>');
  });

  it('shows a saved confirmed snapshot without its internal proof warning', () => {
    const row = {
      ticker: 'AAA.US', name: 'Alpha', market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
      currency: 'USD', L: 10, M: 20, H: 30, option: null, candidate: null, axes: [], ref_axes: [],
      consensus: null, band: null, quote_error: null, error: null, archived: false,
      framework_updated: null, last_earnings: null, next_earnings: null, stale_after_earnings: false,
      valuation_status: 'confirmed_snapshot', historical_evidence_flags: ['confirmed_snapshot_unverified'],
    } as TickerRow;
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).not.toContain('已确认快照，当前执行证明不可用');
    expect(html).toContain('20.00');
  });
  it('shows a saved legacy interval without migration bookkeeping', () => {
    const row = {
      ticker: 'AAA.US', name: 'Alpha', market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
      currency: 'USD', L: 10, M: 20, H: 30, option: null, candidate: null, axes: [], ref_axes: [],
      consensus: null, band: null, quote_error: null, error: null, archived: false,
      framework_updated: '2025-01-01', last_earnings: null, next_earnings: null, stale_after_earnings: true,
      valuation_status: 'legacy_accepted',
    } as TickerRow;
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).not.toContain('历史框架沿用');
    expect(html).toContain('估值区间');
    expect(html).not.toContain('研究已更新');
  });
  it.each([
    ['missing', '暂无估值结果'],
    ['unconfirmed', '估值结果待确认'],
    ['stale', '估值依据已变化'],
    ['invalid', '估值结果无效'],
    ['proof_unavailable', '估值结果暂不可验证'],
    ['unavailable', '估值结果暂不可读取'],
  ])('shows %s result status even when a quote exists', (status, message) => {
    const row = {
      ticker: 'AAA.US', name: 'Alpha', market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
      currency: null, L: null, M: null, H: null, option: null, candidate: null, axes: [], ref_axes: [],
      consensus: null, band: { price: 25, session: null, as_of: '2026-09-21T10:00:00+08:00',
        zone: null, option_zone: null, candidate_zone: null }, quote_error: null, error: null, archived: false,
      framework_updated: null, last_earnings: null, next_earnings: null, stale_after_earnings: false,
      valuation_status: status,
    } as TickerRow;
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain(message);
    expect(html).toContain('25.00');
    expect(html).not.toContain('估值区间：');
  });
  it('keeps confirmed valuation bands visible when the current quote is unavailable', () => {
    const row: TickerRow = {
      ticker: 'AAA.US', name: 'Alpha', market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
      currency: 'USD', L: 10, M: 20, H: 40,
      option: { L: 10, M: 25, H: 50 },
      candidate: { label: '新产能', L: 12, M: 22, H: 44 },
      axes: [], ref_axes: [], consensus: null, band: null, quote_error: '取价失败', error: null, archived: false,
      framework_updated: null, last_earnings: null, next_earnings: null, stale_after_earnings: false,
    };
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('取价失败');
    expect(html).toContain('10.00');
    expect(html).toContain('20.00');
    expect(html).toContain('40.00');
    expect(html).toContain('新产能');
    expect(html).toContain('50.00');
    expect(html).toContain('44.00');
  });

  it('does not invent an interval when neither a quote nor an estimate is available', () => {
    const row: TickerRow = {
      ticker: 'AAA.US', name: 'Alpha', market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
      currency: 'USD', L: null, M: null, H: null, option: null, candidate: null, axes: [], ref_axes: [],
      consensus: null, band: null, quote_error: '取价失败', error: null, archived: false,
      framework_updated: null, last_earnings: null, next_earnings: null, stale_after_earnings: false,
    };
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('取价失败');
    expect(html).not.toContain('估值区间：');
  });

  it('does not infer a quote failure from missing valuation data', () => {
    const row: TickerRow = {
      ticker: 'AAA.US', name: 'Alpha', market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
      currency: 'USD', L: null, M: null, H: null, option: null, candidate: null, axes: [], ref_axes: [],
      consensus: null, band: null, quote_error: null, error: null, archived: false,
      framework_updated: null, last_earnings: null, next_earnings: null, stale_after_earnings: false,
    };
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('估值结果不可用');
    expect(html).not.toContain('无报价');
  });

  it('does not say the quote is missing when only the valuation zone is unavailable', () => {
    const row: TickerRow = {
      ticker: 'AAA.US', name: 'Alpha', market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
      currency: 'USD', L: 10, M: 20, H: 40, option: null, candidate: null, axes: [], ref_axes: [],
      consensus: null, band: { price: 25, session: null, as_of: '2026-09-21T10:00:00+08:00',
        zone: null, option_zone: null, candidate_zone: null }, quote_error: null, error: null, archived: false,
      framework_updated: null, last_earnings: null, next_earnings: null, stale_after_earnings: false,
    };
    const html = renderToStaticMarkup(<TickerCard row={row} label={(id) => id} axes={new Map()}
      onOpen={() => {}} onOpenAxis={() => {}} />);
    expect(html).toContain('估值档位不可用');
    expect(html).not.toContain('无报价');
  });
});
