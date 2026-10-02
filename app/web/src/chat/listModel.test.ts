import { describe, expect, it } from 'vitest';
import { groupChats, relativeTime, tickerLabel } from './listModel';
import type { ChatSummary } from './types';

const chat = (id: string, over: Partial<ChatSummary>): ChatSummary => ({
  id, kind: 'ticker', ticker: 'NVDA.US', title: id, model: 'm', effort: 'e', created_at: 0, updated_at: 0,
  context_window: null, archived: false, state: 'idle', ...over,
});
const names = new Map([['NVDA.US', 'NVIDIA'], ['0700.HK', '腾讯控股']]);

describe('groupChats', () => {
  const chats = [
    chat('r1', { kind: 'research', ticker: null, title: '研究宁德时代', updated_at: 50 }),
    chat('n1', { updated_at: 10 }),
    chat('n2', { updated_at: 30, state: 'running' }),
    chat('t1', { ticker: '0700.HK', updated_at: 40 }),
    chat('landed', { kind: 'research', ticker: '0700.HK', updated_at: 5 }),
    chat('d1', { kind: 'discover', ticker: null, title: 'KOL 看了啥', updated_at: 60 }),
  ];
  it('puts unlanded research on top and groups the rest by ticker, newest first', () => {
    const v = groupChats(chats, '', names);
    expect(v.research.map((c) => c.id)).toEqual(['r1']);
    expect(v.groups.map((g) => [g.ticker, g.chats.map((c) => c.id), g.running, g.updated_at])).toEqual([
      ['0700.HK', ['t1', 'landed'], false, 40],
      ['NVDA.US', ['n2', 'n1'], true, 30],
    ]);
  });
  it('searches by code or name, and research by title', () => {
    expect(groupChats(chats, 'nvid', names).groups.map((g) => g.ticker)).toEqual(['NVDA.US']);
    expect(groupChats(chats, '0700', names).groups.map((g) => g.ticker)).toEqual(['0700.HK']);
    expect(groupChats(chats, '腾讯', names).groups.map((g) => g.ticker)).toEqual(['0700.HK']);
    expect(groupChats(chats, '宁德', names).research.map((c) => c.id)).toEqual(['r1']);
    expect(groupChats(chats, '宁德', names).groups).toEqual([]);
  });
  it('lists discover chats on their own, apart from research', () => {
    const v = groupChats(chats, '', names);
    expect(v.discover.map((c) => c.id)).toEqual(['d1']);
    expect(v.research.map((c) => c.id)).toEqual(['r1']);
    expect(groupChats(chats, 'kol', names).discover.map((c) => c.id)).toEqual(['d1']);
  });
});

describe('tickerLabel', () => {
  it('shows HK / A shares by Chinese name and US tickers by code', () => {
    expect(tickerLabel({ ticker: '0700.HK', name: '腾讯控股 / Tencent Holdings Ltd.', market: 'HK' })).toBe('腾讯控股');
    expect(tickerLabel({ ticker: '600309.SH', name: '万华化学 / Wanhua Chemical Group', market: 'A' })).toBe('万华化学');
    expect(tickerLabel({ ticker: 'AMZN.US', name: '亚马逊 / Amazon.com, Inc.', market: 'US' })).toBe('AMZN.US');
  });
  it('falls back to the code when there is no name', () => {
    expect(tickerLabel({ ticker: '0883.HK', name: null, market: 'HK' })).toBe('0883.HK');
    expect(tickerLabel({ ticker: '0883.HK', name: '', market: 'HK' })).toBe('0883.HK');
  });
});

describe('relativeTime', () => {
  const now = new Date(2026, 8, 27, 12, 0, 0).getTime() / 1000;
  it('reads like a chat list', () => {
    expect(relativeTime(now - 20, now)).toBe('刚刚');
    expect(relativeTime(now - 5 * 60, now)).toBe('5 分钟前');
    expect(relativeTime(now - 3 * 3600, now)).toBe('3 小时前');
    expect(relativeTime(now - 26 * 3600, now)).toBe('昨天');
    expect(relativeTime(new Date(2026, 8, 20, 9).getTime() / 1000, now)).toBe('9/20');
  });
});
