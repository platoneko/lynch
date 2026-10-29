import { describe, expect, it } from 'vitest';
import type { AnomalyItem } from '../anomalies/types';
import { anomalyPrompt, earningsPrompt, withPrompt } from './prompts';
import type { EarningsItem } from './types';

const item = (o: Partial<EarningsItem> = {}): EarningsItem => ({
  ticker: 'TSM.US', status: 'released', date: '2026-10-14', day_offset: 0, slot: 'after',
  release_at: '2026-10-14T16:00:00-04:00', period: '2026 财年第 3 季度', researched: false, ignored: false,
  anomaly: null, ...o,
});

// A local factory: importing another test file would re-run its tests here.
const anomaly = (o: Partial<AnomalyItem> = {}): AnomalyItem => ({
  ticker: 'WOLF.US', ref: '31.37', first_at: '2026-10-08T04:05:00+08:00',
  move: 0.291, mult: 4.13, session: 'post', at: '2026-10-08T04:20:00+08:00',
  reason: 'move', volume_mult: null, researched: false, ignored: false, ...o,
});

describe('anomaly prompts', () => {
  it('anomaly prompt names the move, its base and the no-write rule', () => {
    const p = anomalyPrompt(anomaly());
    expect(p).toContain('WOLF.US 在 10-08 04:20 盘后 出现异动：相对基准收盘 31.37 +29.1%（约平时波动的 4.1 倍）');
    expect(anomalyPrompt(anomaly({ session: 'regular' }))).toContain('WOLF.US 在 10-08 04:20 出现异动');
    expect(p).toContain('frameworks/WOLF.US.md');
    expect(p).toContain('落盘等我说「可以落盘」');
  });
  it('a volume anomaly states the volume multiple', () => {
    expect(anomalyPrompt(anomaly({ reason: 'volume', volume_mult: 5.34, session: 'regular' })))
      .toContain('；当天成交量是平时的 5.3 倍');
  });
  it('post-earnings prompt carries a filed anomaly', () => {
    const p = earningsPrompt(item({ anomaly: anomaly() }));
    expect(p.split('\n')[1]).toBe('财报前后出现异动：10-08 04:20 盘后 相对基准收盘 31.37 +29.1%（约平时波动的 4.1 倍），请一并解释。');
  });
});

describe('earnings prompts', () => {
  it('post-earnings prompt names the ticker, period, date and the no-write rule', () => {
    const p = earningsPrompt(item());
    expect(p).toContain('TSM.US 的 2026 财年第 3 季度财报已于 2026-10-14 盘后发布');
    expect(p).toContain('frameworks/TSM.US.md');
    expect(p).toContain('确认 / 削弱 / 证伪');
    expect(p).toContain('落盘等我说「可以落盘」');
  });
  it('every research prompt asks for a TL;DR before the details', () => {
    expect(earningsPrompt(item())).toContain('回复开头先写 TL;DR');
    expect(earningsPrompt(item({ status: 'upcoming' }))).toContain('回复开头先写 TL;DR');
    expect(anomalyPrompt(anomaly())).toContain('回复开头先写 TL;DR');
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
  it('does not stack the prompt when 调研 is tapped again', () => {
    expect(withPrompt('P', 'P')).toBe('P');
    expect(withPrompt('先看毛利率\n\nP', 'P')).toBe('先看毛利率\n\nP');
  });
});
