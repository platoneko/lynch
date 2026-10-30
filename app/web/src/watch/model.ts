import type { WatchCard, WatchItem, WatchList } from './types';

/** Due and not yet looked at again: what the ⋯ badge and the menu count. */
export function isPending(it: WatchItem): boolean {
  return it.due && !it.researched;
}

export function pendingCount(list: WatchList | null): number {
  return list ? list.items.filter(isPending).length : 0;
}

/** The ⋯ row: what fell due, then what still waits; null drops it while the page would list nothing. */
export function menuLabel(list: WatchList | null): string | null {
  if (!list || list.items.length === 0) return null;
  const pending = pendingCount(list);
  const waiting = list.items.filter((it) => !it.due).length;
  return ['观察', ...(pending > 0 ? [`${pending} 到期`] : []), ...(waiting > 0 ? [`${waiting} 等待`] : [])].join(' · ');
}

/** "11-07 到期 · 10-Q（预计 11 月上旬）；卖方刷新 FY27 每股收益": the row's second line. */
export function rowSub(it: WatchItem): string {
  return `${it.due_date.slice(5)} 到期 · ${it.waits.map((w) => w.what).join('；')}`;
}

/** Whether `saved` is this very card, saved already (the same reply's 加入观察区 tapped before). */
export function sameCard(card: WatchCard, saved: WatchCard): boolean {
  return card.recheck === saved.recheck && JSON.stringify(card.waits) === JSON.stringify(saved.waits);
}

/** The warning under a card whose ticker already has another one on the list. */
export function replacesText(saved: WatchCard): string {
  const first = saved.waits[0];
  return `存入会覆盖观察区里原有的卡片（原：${first.date.slice(5)} ${first.what}${saved.waits.length > 1 ? ' 等' : ''}）`;
}
