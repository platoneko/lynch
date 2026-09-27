import { isBusy } from './chatModel';
import type { ChatSummary } from './types';

export interface ChatGroup {
  ticker: string;
  chats: ChatSummary[]; // newest first
  running: boolean;
  updated_at: number;
}

export interface ChatListView {
  research: ChatSummary[]; // new-ticker research not yet landed
  groups: ChatGroup[]; // newest activity first
}

export function groupChats(chats: ChatSummary[], query: string, names: ReadonlyMap<string, string>): ChatListView {
  const q = query.trim().toLowerCase();
  const byTime = [...chats].sort((a, b) => b.updated_at - a.updated_at);
  const research = byTime.filter((c) => c.ticker === null && (!q || c.title.toLowerCase().includes(q)));
  const groups = new Map<string, ChatSummary[]>();
  for (const c of byTime) {
    if (c.ticker === null) continue;
    const name = names.get(c.ticker)?.toLowerCase() ?? '';
    if (q && !c.ticker.toLowerCase().includes(q) && !name.includes(q)) continue;
    groups.set(c.ticker, [...(groups.get(c.ticker) ?? []), c]);
  }
  return {
    research,
    groups: [...groups].map(([ticker, list]) => ({
      ticker, chats: list, running: list.some((c) => isBusy(c.state)), updated_at: list[0].updated_at,
    })),
  };
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
