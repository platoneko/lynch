import type { TickerRow } from '../types';
import { isBusy } from './chatModel';
import type { ChatSummary } from './types';

export interface ChatGroup {
  ticker: string;
  chats: ChatSummary[]; // newest first
  running: boolean;
  updated_at: number;
}

export interface ChatListView {
  discover: ChatSummary[]; // Grok searches of the KOL list, newest first
  research: ChatSummary[]; // new-ticker research not yet landed
  groups: ChatGroup[]; // newest activity first
}

export function groupChats(chats: ChatSummary[], query: string, names: ReadonlyMap<string, string>): ChatListView {
  const q = query.trim().toLowerCase();
  const byTime = [...chats].sort((a, b) => b.updated_at - a.updated_at);
  const titled = (c: ChatSummary) => !q || c.title.toLowerCase().includes(q);
  const discover = byTime.filter((c) => c.kind === 'discover' && titled(c));
  const research = byTime.filter((c) => c.kind !== 'discover' && c.ticker === null && titled(c));
  const groups = new Map<string, ChatSummary[]>();
  for (const c of byTime) {
    if (c.ticker === null || !matchesTicker(c.ticker, query, names)) continue;
    groups.set(c.ticker, [...(groups.get(c.ticker) ?? []), c]);
  }
  return {
    discover,
    research,
    groups: [...groups].map(([ticker, list]) => ({
      ticker, chats: list, running: list.some((c) => isBusy(c.state)), updated_at: list[0].updated_at,
    })),
  };
}

/** A ticker search box's rule: the query, case-insensitive, is part of the code or of the framework's full name. */
export function matchesTicker(ticker: string, query: string, names: ReadonlyMap<string, string>): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return ticker.toLowerCase().includes(q) || (names.get(ticker)?.toLowerCase() ?? '').includes(q);
}

/** HK / A-share codes are bare numbers, so they read by Chinese name; US tickers are readable as is. */
export function tickerLabel(row: Pick<TickerRow, 'ticker' | 'name' | 'market'>): string {
  if (row.market !== 'HK' && row.market !== 'A') return row.ticker;
  const zh = row.name?.split(' / ')[0].trim(); // title name is "中文名 / English Name"
  return zh || row.ticker;
}

export function relativeTime(ts: number, now: number): string {
  const s = now - ts;
  if (s < 60) return '刚刚';
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  const then = new Date(ts * 1000);
  const today = new Date(now * 1000);
  const days = Math.round((startOfDay(today) - startOfDay(then)) / 86_400_000);
  if (days === 0) return `${Math.floor(s / 3600)} 小时前`;
  if (days === 1) return '昨天';
  return `${then.getMonth() + 1}/${then.getDate()}`;
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}
