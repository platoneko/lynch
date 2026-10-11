import type { EarningsItem, EarningsList, EarningsSlot } from './types';

export const SLOT_CN: Record<EarningsSlot, string> = { before: '盘前', during: '盘中', after: '盘后', unknown: '' };
const NEAR_DAY: Record<number, string> = { [-1]: '昨天', 0: '今天', 1: '明天' };

/** Out, and neither researched nor dismissed: what the ⋯ badge and the menu count. */
export function isPending(it: EarningsItem): boolean {
  return it.status === 'released' && !it.researched && !it.ignored;
}

export function pendingCount(list: EarningsList | null): number {
  return list ? list.items.filter(isPending).length : 0;
}

export function menuLabel(n: number): string {
  return n > 0 ? `财报 · ${n} 待看` : '财报';
}

export function whenText(it: EarningsItem): string {
  return `${NEAR_DAY[it.day_offset] ?? it.date.slice(5)}${SLOT_CN[it.slot]}`;
}

export function doneTag(it: EarningsItem): string | null {
  if (it.researched) return '已调研';
  if (it.ignored) return '已忽略';
  return null;
}
