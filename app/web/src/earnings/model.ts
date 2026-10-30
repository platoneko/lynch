import { matchesTicker } from '../chat/listModel';
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

/** The ⋯ row: what waits, then what is still to come; null drops it while the page would list nothing. */
export function menuLabel(list: EarningsList | null): string | null {
  if (!list || list.items.length === 0) return null;
  const pending = pendingCount(list);
  const upcoming = list.items.filter((it) => it.status === 'upcoming').length;
  return ['财报', ...(pending > 0 ? [`${pending} 待看`] : []), ...(upcoming > 0 ? [`${upcoming} 将出`] : [])]
    .join(' · ');
}

/** The page's search box: same rule as the chat list's, so one query finds a ticker on both. */
export function searchEarnings(items: EarningsItem[], query: string, names: ReadonlyMap<string, string>): EarningsItem[] {
  return items.filter((it) => matchesTicker(it.ticker, query, names));
}

export function whenText(it: EarningsItem): string {
  return `${NEAR_DAY[it.day_offset] ?? it.date.slice(5)}${SLOT_CN[it.slot]}`;
}

export { doneTag } from '../alerts/model';
