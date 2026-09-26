import { describe, expect, it } from 'vitest';
import type { TickerRow, ZoneId } from '../types';
import { applyFilter, DEFAULT_FILTER, latestAsOf, lynchClasses, quoteTimeText } from './listModel';

const ZONES = ['deep_discount', 'conservative', 'neutral', 'optimistic', 'stretched'];

function row(ticker: string, over: Partial<TickerRow> = {}): TickerRow {
  return {
    ticker, name: `${ticker} name`, market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
    currency: 'USD', L: 10, M: 20, H: 40, blocked: null, candidate: null,
    band: { price: 25, as_of: '2026-09-26T09:03:00+08:00', zone: 'neutral', option_zone: null, candidate_zone: null },
    quote_error: null, error: null, ...over,
  };
}
const withZone = (t: string, zone: ZoneId | null) =>
  row(t, { band: { price: 1, as_of: '2026-09-26T09:03:00+08:00', zone, option_zone: null, candidate_zone: null } });
const tickers = (rows: TickerRow[]) => rows.map((r) => r.ticker);

describe('search', () => {
  const rows = [row('NVDA.US', { name: '英伟达' }), row('0700.HK', { name: '腾讯控股', market: 'HK' })];
  it('matches ticker case-insensitively and trims', () => {
    expect(tickers(applyFilter(rows, { ...DEFAULT_FILTER, query: ' nvda ' }, ZONES))).toEqual(['NVDA.US']);
  });
  it('matches name substring', () => {
    expect(tickers(applyFilter(rows, { ...DEFAULT_FILTER, query: '腾讯' }, ZONES))).toEqual(['0700.HK']);
    expect(tickers(applyFilter(rows, { ...DEFAULT_FILTER, query: '0700' }, ZONES))).toEqual(['0700.HK']);
  });
  it('does not crash on error rows without a name', () => {
    const broken = row('BAD.US', { name: null, error: 'boom', band: null });
    expect(tickers(applyFilter([broken], { ...DEFAULT_FILTER, query: 'x' }, ZONES))).toEqual([]);
  });
});

describe('filters', () => {
  it('zone filter is multi-select and drops rows without a zone', () => {
    const rows = [withZone('A.US', 'neutral'), withZone('B.US', 'stretched'), withZone('C.US', 'conservative'),
      row('D.US', { band: null }), withZone('E.US', null)];
    const f = { ...DEFAULT_FILTER, zones: ['neutral', 'conservative'] };
    expect(tickers(applyFilter(rows, f, ZONES))).toEqual(['C.US', 'A.US']);
  });
  it('BLOCKED rows with a stale band zone are excluded by the zone filter', () => {
    // Marked BLOCKED after the last daily run: the band file still holds yesterday's zone.
    const rows = [withZone('A.US', 'neutral'), row('B.US', { blocked: 'reason' })];
    const f = { ...DEFAULT_FILTER, zones: ['neutral'] };
    expect(tickers(applyFilter(rows, f, ZONES))).toEqual(['A.US']);
  });
  it('class filter matches primary or secondary', () => {
    const rows = [row('A.US', { lynch_primary: 'cyclical' }),
      row('B.US', { lynch_primary: 'fast_grower', lynch_secondary: 'cyclical' }),
      row('C.US', { lynch_primary: 'stalwart' })];
    expect(tickers(applyFilter(rows, { ...DEFAULT_FILTER, classes: ['cyclical'] }, ZONES))).toEqual(['A.US', 'B.US']);
  });
  it('market filter is multi-select and drops rows with an unknown market', () => {
    const rows = [row('A.US'), row('0700.HK', { market: 'HK' }), row('601899.SH', { market: 'A' }),
      row('X.XX', { market: null })];
    const f = { ...DEFAULT_FILTER, sort: 'ticker' as const, markets: ['A', 'HK'] };
    expect(tickers(applyFilter(rows, f, ZONES))).toEqual(['0700.HK', '601899.SH']);
  });
});

describe('sort', () => {
  const rows = [withZone('Z.US', 'stretched'), row('N.US', { band: null }), withZone('A.US', 'stretched'),
    withZone('M.US', 'deep_discount'), row('X.US', { error: 'boom', band: null, name: null })];
  it('zone order, zone-less last, ticker as tiebreak', () => {
    expect(tickers(applyFilter(rows, DEFAULT_FILTER, ZONES))).toEqual(['M.US', 'A.US', 'Z.US', 'N.US', 'X.US']);
  });
  it('BLOCKED rows with a stale band zone sort after zoned rows', () => {
    const blocked = row('A.US', { blocked: 'reason' }); // band.zone 'neutral' left over
    expect(tickers(applyFilter([blocked, withZone('Z.US', 'stretched')], DEFAULT_FILTER, ZONES)))
      .toEqual(['Z.US', 'A.US']);
  });
  it('market keeps the server order (US → HK → A)', () => {
    expect(tickers(applyFilter(rows, { ...DEFAULT_FILTER, sort: 'market' }, ZONES)))
      .toEqual(['Z.US', 'N.US', 'A.US', 'M.US', 'X.US']);
  });
  it('ticker is plain alphabetical', () => {
    expect(tickers(applyFilter(rows, { ...DEFAULT_FILTER, sort: 'ticker' }, ZONES)))
      .toEqual(['A.US', 'M.US', 'N.US', 'X.US', 'Z.US']);
  });
});

describe('latestAsOf', () => {
  it('picks the newest timestamp', () => {
    const rows = [row('A.US'), row('B.US', { band: { price: 1, as_of: '2026-09-26T16:13:00+08:00', zone: null, option_zone: null, candidate_zone: null } })];
    expect(latestAsOf(rows)).toBe('2026-09-26T16:13:00+08:00');
  });
  it('is null without any band', () => {
    expect(latestAsOf([row('A.US', { band: null })])).toBeNull();
  });
});

describe('quoteTimeText', () => {
  it('shows month-day and minute of the quote time as sent (Asia/Shanghai)', () => {
    expect(quoteTimeText('2026-09-26T16:13:05+08:00')).toBe('取价 09-26 16:13');
  });
  it('says so when nothing was priced', () => {
    expect(quoteTimeText(null)).toBe('未取到价');
  });
});

describe('lynchClasses', () => {
  const labels = new Map([['fast_grower', '快速增长'], ['cyclical', '周期']]);
  const label = (id: string) => labels.get(id) ?? id;
  it('shows the Chinese labels, primary first', () => {
    expect(lynchClasses(row('A', { lynch_primary: 'fast_grower', lynch_secondary: 'cyclical' }), label))
      .toBe('快速增长 / 周期');
    expect(lynchClasses(row('A', { lynch_primary: 'cyclical' }), label)).toBe('周期');
  });
  it('falls back to the id for an unknown class', () => {
    expect(lynchClasses(row('A', { lynch_primary: 'new_kind' }), label)).toBe('new_kind');
  });
});
