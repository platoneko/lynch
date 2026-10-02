import { describe, expect, it } from 'vitest';
import type { TickerRow, ZoneId } from '../types';
import { applyFilter, consensusText, DEFAULT_FILTER, extraBands, inArchiveScope, latestAsOf, lynchClasses, quoteTimeText } from './listModel';

const ZONES = ['deep_discount', 'conservative', 'neutral', 'optimistic', 'stretched'];

function row(ticker: string, over: Partial<TickerRow> = {}): TickerRow {
  return {
    ticker, name: `${ticker} name`, market: 'US', lynch_primary: 'stalwart', lynch_secondary: null,
    currency: 'USD', L: 10, M: 20, H: 40, option: null, candidate: null, axes: [], ref_axes: [], consensus: null,
    band: { price: 25, session: null, as_of: '2026-09-26T09:03:00+08:00', zone: 'neutral', option_zone: null, candidate_zone: null },
    quote_error: null, error: null, archived: false, ...over,
  };
}
const withZone = (t: string, zone: ZoneId | null) =>
  row(t, { band: { price: 1, session: null, as_of: '2026-09-26T09:03:00+08:00', zone, option_zone: null, candidate_zone: null } });
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
  it('class filter matches primary or secondary', () => {
    const rows = [row('A.US', { lynch_primary: 'cyclical' }),
      row('B.US', { lynch_primary: 'fast_grower', lynch_secondary: 'cyclical' }),
      row('C.US', { lynch_primary: 'stalwart' })];
    expect(tickers(applyFilter(rows, { ...DEFAULT_FILTER, classes: ['cyclical'] }, ZONES))).toEqual(['A.US', 'B.US']);
  });
  it('axis filter matches any selected axis; a parse-error row has none', () => {
    const rows = [row('A.US', { axes: ['al', 'cu'] }), row('B.US', { axes: ['li'] }), row('C.US'),
      row('D.US', { axes: null })];
    const f = { ...DEFAULT_FILTER, sort: 'ticker' as const, axes: ['cu', 'li'] };
    expect(tickers(applyFilter(rows, f, ZONES))).toEqual(['A.US', 'B.US']);
  });
  it('axis filter also keeps tickers that only reference the axis', () => {
    const rows = [row('A.US', { axes: ['cu'] }), row('B.US', { ref_axes: ['cu'] }), row('C.US', { ref_axes: ['al'] }),
      row('D.US', { ref_axes: null })];
    const f = { ...DEFAULT_FILTER, sort: 'ticker' as const, axes: ['cu'] };
    expect(tickers(applyFilter(rows, f, ZONES))).toEqual(['A.US', 'B.US']);
  });
  it('market filter is multi-select and drops rows with an unknown market', () => {
    const rows = [row('A.US'), row('0700.HK', { market: 'HK' }), row('601899.SH', { market: 'A' }),
      row('X.XX', { market: null })];
    const f = { ...DEFAULT_FILTER, sort: 'ticker' as const, markets: ['A', 'HK'] };
    expect(tickers(applyFilter(rows, f, ZONES))).toEqual(['0700.HK', '601899.SH']);
  });
});

describe('archive', () => {
  const rows = [row('A.US'), row('B.US', { archived: true }), row('C.US', { archived: true, error: 'boom', band: null })];
  it('hides archived rows by default', () => {
    expect(tickers(applyFilter(rows, DEFAULT_FILTER, ZONES))).toEqual(['A.US']);
  });
  it('shows them when asked, under the other filters', () => {
    expect(tickers(applyFilter(rows, { ...DEFAULT_FILTER, showArchived: true }, ZONES))).toEqual(['A.US', 'B.US', 'C.US']);
    expect(tickers(applyFilter(rows, { ...DEFAULT_FILTER, showArchived: true, query: 'b.' }, ZONES))).toEqual(['B.US']);
  });
  it('scopes the list total the same way', () => {
    expect(rows.filter((r) => inArchiveScope(r, DEFAULT_FILTER)).length).toBe(1);
    expect(rows.filter((r) => inArchiveScope(r, { ...DEFAULT_FILTER, showArchived: true })).length).toBe(3);
  });
});

describe('sort', () => {
  const rows = [withZone('Z.US', 'stretched'), row('N.US', { band: null }), withZone('A.US', 'stretched'),
    withZone('M.US', 'deep_discount'), row('X.US', { error: 'boom', band: null, name: null })];
  it('zone order, zone-less last, ticker as tiebreak', () => {
    expect(tickers(applyFilter(rows, DEFAULT_FILTER, ZONES))).toEqual(['M.US', 'A.US', 'Z.US', 'N.US', 'X.US']);
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
    const rows = [row('A.US'), row('B.US', { band: { price: 1, session: null, as_of: '2026-09-26T16:13:00+08:00', zone: null, option_zone: null, candidate_zone: null } })];
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

describe('extraBands', () => {
  const option = { v_opt: 18.17, L: 10, M: 25, H: 58.17, line: null, rows: [] };
  const candidate = { label: 'FY28', L: 12, M: 22, H: 44, gate: null, background: null };
  const zones = { price: 25, session: null, as_of: '2026-09-26T09:03:00+08:00', zone: 'neutral', option_zone: 'conservative',
    candidate_zone: 'optimistic' } as const;
  it('orders +V before the candidate, each with its own zone and label', () => {
    const bands = extraBands(row('A.US', { option, candidate, band: zones }));
    expect(bands.map((b) => [b.kind, b.label, b.zone, b.anchors.H])).toEqual([
      ['option', '+V 18.17', 'conservative', 58.17],
      ['candidate', 'FY28', 'optimistic', 44],
    ]);
  });
  it('formats V_opt like the price labels', () => {
    expect(extraBands(row('A.US', { option: { ...option, v_opt: 4 } }))[0].label).toBe('+V 4.00');
  });
  it('keeps a band whose zone is missing, with a null zone', () => {
    expect(extraBands(row('A.US', { option, band: null })).map((b) => b.zone)).toEqual([null]);
  });
  it('is empty without an option or a candidate', () => {
    expect(extraBands(row('A.US'))).toEqual([]);
  });
});

describe('consensusText', () => {
  it('shows the tier or that none is written', () => {
    expect(consensusText({ tier: 'M–H', note: 'x' })).toBe('共识 M–H');
    expect(consensusText(null)).toBe('共识未标注');
  });
});
