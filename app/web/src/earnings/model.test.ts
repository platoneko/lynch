import { describe, expect, it } from 'vitest';
import { doneTag, isPending, menuLabel, pendingCount, searchEarnings, whenText } from './model';
import type { EarningsItem } from './types';

const item = (o: Partial<EarningsItem> = {}): EarningsItem => ({
  ticker: 'TSM.US', status: 'released', date: '2026-10-14', day_offset: 0, slot: 'after',
  release_at: '2026-10-14T16:00:00-04:00', period: '2026 财年第 3 季度', researched: false, ignored: false,
  anomaly: null, ...o,
});

describe('earnings model', () => {
  it('counts only released, unresearched, unignored rows', () => {
    const items = [item(), item({ researched: true }), item({ ignored: true }), item({ status: 'upcoming' })];
    expect(items.map(isPending)).toEqual([true, false, false, false]);
    expect(pendingCount({ fetched_day: '2026-10-14', error: null, items })).toBe(1);
    expect(pendingCount(null)).toBe(0);
  });
  it('labels the menu row, and drops it while the page would be empty', () => {
    const list = (items: EarningsItem[]) => ({ fetched_day: '2026-10-14', error: null, items });
    expect(menuLabel(list([item({ researched: true })]))).toBe('财报');
    expect(menuLabel(list([item(), item({ ticker: 'AAPL.US' })]))).toBe('财报 · 2 待看');
    expect(menuLabel(list([item({ status: 'upcoming' })]))).toBe('财报 · 1 将出');
    expect(menuLabel(list([item(), item({ ticker: 'TSM.US', status: 'upcoming' })]))).toBe('财报 · 1 待看 · 1 将出');
    expect(menuLabel(list([]))).toBeNull();
    expect(menuLabel(null)).toBeNull();
  });
  it('says when, relative near today', () => {
    expect(whenText(item({ day_offset: 0, slot: 'after' }))).toBe('今天盘后');
    expect(whenText(item({ day_offset: 1, slot: 'before' }))).toBe('明天盘前');
    expect(whenText(item({ day_offset: -1, slot: 'during' }))).toBe('昨天盘中');
    expect(whenText(item({ day_offset: -3, date: '2026-10-11', slot: 'unknown' }))).toBe('10-11');
  });
  it('searches by code or full name, keeping the server order', () => {
    const items = [item({ ticker: 'TSM.US' }), item({ ticker: '0700.HK', status: 'upcoming' }), item({ ticker: 'NVDA.US' })];
    const names = new Map([['TSM.US', 'Taiwan Semiconductor'], ['0700.HK', '腾讯控股 / Tencent']]);
    const tickers = (q: string) => searchEarnings(items, q, names).map((it) => it.ticker);
    expect(tickers('')).toEqual(['TSM.US', '0700.HK', 'NVDA.US']);
    expect(tickers(' tsm ')).toEqual(['TSM.US']);
    expect(tickers('0700')).toEqual(['0700.HK']);
    expect(tickers('腾讯')).toEqual(['0700.HK']);
    expect(tickers('tencent')).toEqual(['0700.HK']);
    expect(tickers('.us')).toEqual(['TSM.US', 'NVDA.US']);
    expect(tickers('苹果')).toEqual([]);
  });
  it('tags handled rows', () => {
    expect(doneTag(item({ researched: true, ignored: true }))).toBe('已调研');
    expect(doneTag(item({ ignored: true }))).toBe('已忽略');
    expect(doneTag(item())).toBeNull();
  });
});
