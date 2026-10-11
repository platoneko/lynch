import { describe, expect, it } from 'vitest';
import { earningsPrompt, withPrompt } from './prompts';
import type { EarningsItem } from './types';

const item = (o: Partial<EarningsItem> = {}): EarningsItem => ({
  ticker: 'TSM.US', status: 'released', date: '2026-10-14', day_offset: 0, slot: 'after',
  release_at: '2026-10-14T16:00:00-04:00', period: '2026 财年第 3 季度', researched: false, ignored: false, ...o,
});

describe('earnings prompts', () => {
  it('post-earnings prompt names the ticker, period, date and the no-write rule', () => {
    const p = earningsPrompt(item());
    expect(p).toContain('TSM.US 的 2026 财年第 3 季度财报已于 2026-10-14 盘后发布');
    expect(p).toContain('frameworks/TSM.US.md');
    expect(p).toContain('确认 / 削弱 / 证伪');
    expect(p).toContain('落盘等我说「可以落盘」');
  });
  it('pre-earnings prompt for an upcoming release', () => {
    const p = earningsPrompt(item({ status: 'upcoming', slot: 'before', date: '2026-10-16' }));
    expect(p).toContain('TSM.US 将于 2026-10-16 盘前发布 2026 财年第 3 季度财报');
    expect(p).toContain('一致预期');
    expect(p).not.toContain('可以落盘');
  });
  it('an unknown slot says only the date', () => {
    expect(earningsPrompt(item({ slot: 'unknown' }))).toContain('已于 2026-10-14 发布');
  });
  it('keeps an unsent draft and appends the prompt', () => {
    expect(withPrompt('', 'P')).toBe('P');
    expect(withPrompt('   \n', 'P')).toBe('P');
    expect(withPrompt('先看毛利率\n', 'P')).toBe('先看毛利率\n\nP');
  });
});
