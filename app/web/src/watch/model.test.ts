import { describe, expect, it } from 'vitest';
import { menuLabel, pendingCount, replacesText, rowSub, sameCard } from './model';
import type { WatchItem } from './types';

const item = (o: Partial<WatchItem> = {}): WatchItem => ({
  ticker: 'SOFI.US', recheck: '分部营销费', saved_at: 1, conv_id: 'c1',
  waits: [{ what: '10-Q（预计 11 月上旬）', date: '2026-11-07' }, { what: '卖方刷新', date: '2026-11-10' }],
  due_date: '2026-11-07', due: false, researched: false, ...o,
});

describe('watch model', () => {
  it('counts due cards not looked at again', () => {
    const list = { items: [item({ due: true }), item({ due: true, researched: true }), item()] };
    expect(pendingCount(list)).toBe(1);
    expect(menuLabel(list)).toBe('观察 · 1 到期 · 1 等待');
  });
  it('drops the menu row while nothing is listed, keeps it bare when all is looked at', () => {
    expect(menuLabel(null)).toBeNull();
    expect(menuLabel({ items: [] })).toBeNull();
    expect(menuLabel({ items: [item({ due: true, researched: true })] })).toBe('观察');
  });
  it('the row line leads with the due day and lists every wait', () => {
    expect(rowSub(item())).toBe('11-07 到期 · 10-Q（预计 11 月上旬）；卖方刷新');
  });
  it('tells a card already saved from one that would replace it', () => {
    const saved = item();
    expect(sameCard({ ...saved }, saved)).toBe(true);
    expect(sameCard({ ...saved, recheck: '别的' }, saved)).toBe(false);
    expect(replacesText(saved)).toBe('存入会覆盖观察区里原有的卡片（原：11-07 10-Q（预计 11 月上旬） 等）');
    expect(replacesText({ ...saved, waits: saved.waits.slice(1) })).toBe('存入会覆盖观察区里原有的卡片（原：11-10 卖方刷新）');
  });
});
