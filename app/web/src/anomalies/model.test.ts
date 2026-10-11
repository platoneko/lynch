import { describe, expect, it } from 'vitest';
import { hitText, isPending, menuLabel, pendingCount } from './model';
import type { AnomalyItem } from './types';

const anomaly = (o: Partial<AnomalyItem> = {}): AnomalyItem => ({
  ticker: 'WOLF.US', ref: '31.37', first_at: '2026-10-08T04:05:00+08:00',
  move: 0.291, mult: 4.13, session: 'post', at: '2026-10-08T04:20:00+08:00',
  reason: 'move', volume_mult: null, researched: false, ignored: false, ...o,
});

describe('anomaly model', () => {
  it('pending means neither researched nor ignored', () => {
    expect(isPending(anomaly())).toBe(true);
    expect(isPending(anomaly({ researched: true }))).toBe(false);
    expect(isPending(anomaly({ ignored: true }))).toBe(false);
    expect(pendingCount({ error: null, items: [anomaly(), anomaly({ ignored: true })] })).toBe(1);
    expect(pendingCount(null)).toBe(0);
  });
  it('menu label counts what waits', () => {
    expect(menuLabel(2)).toBe('异动 · 2 待看');
    expect(menuLabel(0)).toBe('异动');
  });
  it('hit text: move, session, Beijing time, multiple, volume', () => {
    expect(hitText(anomaly())).toBe('+29.1% 盘后 · 10-08 04:20 · 平时波动 4.1 倍');
    expect(hitText(anomaly({ move: -0.063, session: 'regular', reason: 'volume', mult: 2.2, volume_mult: 5.34,
      at: '2026-10-08T23:10:00+08:00' }))).toBe('-6.3% · 10-08 23:10 · 平时波动 2.2 倍 · 放量 5.3 倍');
  });
});
