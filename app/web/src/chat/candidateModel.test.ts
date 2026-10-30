import { describe, expect, it } from 'vitest';
import {
  archiveDaysLeft, bjtTime, candidateDraft, candidateLabel, costText, defaultPicked, discoverModelText, groupCandidates,
  partialText, shownSources, sourceTimeline,
} from './candidateModel';
import { toolSummary } from './chatModel';
import type { Candidate, SavedCandidate } from './types';

const src = { handle: 'alice', stance: 'bullish' as const, reason: '游戏回暖', quote: 'Tencent looks cheap',
  url: 'https://x.com/alice/status/1', posted_at: '2026-10-01T08:00:00Z' };
const hk: Candidate = { ticker: '0700.HK', name: '腾讯控股', sources: [src] };
const us: Candidate = { ticker: 'AXTI.US', name: 'AXT Inc', sources: [src] };
const bare: Candidate = { ticker: 'AXTI.US', name: '', sources: [src] };
const saved = (c: Candidate, archived_at: number | null = null): SavedCandidate => ({
  ...c, sources: c.sources.map((s) => ({ ...s, saved_at: 1, conv_id: 'c' })), first_saved_at: 1, updated_at: 1, archived_at,
});

describe('candidateLabel', () => {
  it('leads HK / A shares with the name, US with the code', () => {
    expect(candidateLabel(hk)).toBe('腾讯控股 · 0700.HK');
    expect(candidateLabel({ ticker: '600519.SH', name: '贵州茅台' })).toBe('贵州茅台 · 600519.SH');
    expect(candidateLabel(us)).toBe('AXTI.US · AXT Inc');
    expect(candidateLabel(bare)).toBe('AXTI.US');
  });
});

describe('defaultPicked', () => {
  it('leaves out tickers with a framework or already in the list', () => {
    const msft: Candidate = { ticker: 'MSFT.US', name: 'Microsoft', sources: [src] };
    const picked = defaultPicked([hk, us, msft], (t) => t === 'MSFT.US', new Set(['AXTI.US']));
    expect([...picked]).toEqual(['0700.HK']);
  });
});

describe('groupCandidates', () => {
  it('puts archived first, then landed, else to-do', () => {
    const msft = { ...us, ticker: 'MSFT.US' };
    const nvda = { ...us, ticker: 'NVDA.US' };
    const g = groupCandidates([saved(hk), saved(us, 5), saved(msft), saved(nvda, 5)], (t) => t === 'MSFT.US' || t === 'NVDA.US');
    expect([g.todo, g.landed, g.archived].map((l) => l.map((c) => c.ticker)))
      .toEqual([['0700.HK'], ['MSFT.US'], ['AXTI.US', 'NVDA.US']]);
  });
});

describe('archiveDaysLeft', () => {
  it('counts down from 30 days, rounding up, never below 0', () => {
    const day = 86400;
    expect(archiveDaysLeft(100, 100)).toBe(30);
    expect(archiveDaysLeft(100, 100 + day + 1)).toBe(29);
    expect(archiveDaysLeft(100, 100 + 30 * day)).toBe(0);
  });
});

describe('shownSources', () => {
  it('keeps each KOL\'s latest post, newest first, at most 5 KOLs', () => {
    const at = (handle: string, day: string) => ({ ...src, handle, url: `https://x.com/${handle}/status/${day}`,
      posted_at: `2026-10-${day}T08:00:00Z` });
    const list = [at('alice', '01'), at('alice', '03'), at('bob', '02'), at('carol', '04'), at('dave', '05'),
      at('erin', '06'), at('frank', '07')];
    expect(shownSources(list).map((s) => `${s.handle}${s.posted_at.slice(8, 10)}`))
      .toEqual(['frank07', 'erin06', 'dave05', 'carol04', 'alice03']);
    expect(shownSources([at('bob', '02'), at('alice', '01')]).map((s) => s.handle)).toEqual(['bob', 'alice']);
  });
});

describe('sourceTimeline', () => {
  it('lists every stored source, newest first', () => {
    const at = (handle: string, day: string) => ({ ...src, handle, posted_at: `2026-10-${day}T08:00:00Z` });
    expect(sourceTimeline([at('alice', '01'), at('bob', '03'), at('alice', '02')]).map((s) => `${s.handle}${s.posted_at.slice(8, 10)}`))
      .toEqual(['bob03', 'alice02', 'alice01']);
  });
});

describe('bjtTime', () => {
  it('shows an ISO time in Beijing time to the minute', () => {
    expect(bjtTime('2026-10-01T20:30:00Z')).toBe('2026-10-02 04:30');
    expect(bjtTime('2026-10-01T20:30:00+00:00')).toBe('2026-10-02 04:30');
    expect(bjtTime('not a time')).toBe('not a time');
  });
});

describe('candidateDraft', () => {
  it('names the ticker and quotes every source', () => {
    expect(candidateDraft(hk)).toBe([
      '调研 腾讯控股 · 0700.HK。', '', 'KOL 线索：',
      '- @alice（看多，2026-10-01 16:00 北京时间）：游戏回暖', '  原文：Tencent looks cheap', '  原帖：https://x.com/alice/status/1',
    ].join('\n'));
  });
});

describe('discover header', () => {
  it('shows model, window and effort, and the cost', () => {
    expect(discoverModelText({ model: 'grok-4.6', effort: 'high', context_window: 500000 })).toBe('grok-4.6-500k · high');
    expect(discoverModelText({ model: 'grok-4.6', effort: 'low', context_window: 256000 })).toBe('grok-4.6-256k · low');
    expect(discoverModelText({ model: 'grok-4.5', effort: 'high', context_window: 500000, lookback_days: 3 }))
      .toBe('grok-4.5-500k · high · 3天');
    expect(costText(0.123)).toBe('本对话 $0.12');
    expect(costText(null)).toBe('');
  });
});

describe('partialText', () => {
  it('hides a card block while it streams', () => {
    expect(partialText('看到两只\n\n```lynch-candidates\n{"cand')).toEqual({ text: '看到两只', collecting: true });
    expect(partialText('还在搜')).toEqual({ text: '还在搜', collecting: false });
    expect(partialText('先等卖方\n```lynch-watch\n{"tic')).toEqual({ text: '先等卖方', collecting: true });
  });
});

describe('grok tools', () => {
  it('names X search', () => {
    expect(toolSummary({ kind: 'tool', key: 't', id: 't', name: 'X search:', input: { value: '{}' }, output: 'x',
      is_error: false, denied: false })).toBe('搜索 X ✓');
  });
});
