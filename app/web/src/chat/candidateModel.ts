import type { Candidate, CandidateSource, ChatSummary, SavedCandidate, Stance } from './types';

export const STANCE_LABEL: Record<Stance, string> = { bullish: '看多', bearish: '看空', neutral: '中性' };
export const DISCOVER_OPENER = '看看名单里 KOL 最近提到了哪些票';

const NAMED_MARKETS = new Set(['HK', 'SH', 'SZ']);
const BLOCK_START = '```lynch-candidates';

/** HK / A-share codes are bare numbers, so their name leads (the server requires one there). */
export function candidateLabel(c: Pick<Candidate, 'ticker' | 'name'>): string {
  const market = c.ticker.slice(c.ticker.lastIndexOf('.') + 1);
  if (NAMED_MARKETS.has(market)) return `${c.name} · ${c.ticker}`;
  return c.name ? `${c.ticker} · ${c.name}` : c.ticker;
}

/** Ticked at first: what neither has a framework nor already waits in the candidate list. */
export function defaultPicked(cands: Candidate[], hasFramework: (t: string) => boolean, saved: ReadonlySet<string>): Set<string> {
  return new Set(cands.filter((c) => !hasFramework(c.ticker) && !saved.has(c.ticker)).map((c) => c.ticker));
}

export interface CandidateGroups {
  todo: SavedCandidate[];
  landed: SavedCandidate[]; // its framework exists now
  archived: SavedCandidate[]; // put away by the user, landed or not
}

export function groupCandidates(list: SavedCandidate[], hasFramework: (t: string) => boolean): CandidateGroups {
  const g: CandidateGroups = { todo: [], landed: [], archived: [] };
  for (const c of list) (c.archived_at !== null ? g.archived : hasFramework(c.ticker) ? g.landed : g.todo).push(c);
  return g;
}

const ARCHIVE_DAYS = 30; // server candidates.py RETENTION_S

/** Whole days an archived ticker has left before the server deletes it. */
export function archiveDaysLeft(archivedAt: number, now: number): number {
  return Math.max(0, Math.ceil(ARCHIVE_DAYS - (now - archivedAt) / 86400));
}

const SHOWN_KOLS = 5;

/** The sources a card shows: each KOL's latest post, newest first, at most SHOWN_KOLS KOLs. All stay stored. */
export function shownSources<S extends CandidateSource>(sources: S[]): S[] {
  const time = (s: S) => Date.parse(s.posted_at) || 0;
  const latest = new Map<string, S>();
  for (const s of sources) {
    const known = latest.get(s.handle);
    if (!known || time(s) > time(known)) latest.set(s.handle, s);
  }
  return [...latest.values()].sort((a, b) => time(b) - time(a)).slice(0, SHOWN_KOLS);
}

const BJT_OFFSET_MS = 8 * 3600_000; // every time the user reads is Beijing time

/** An ISO time as Beijing time to the minute (2026-10-02 04:30); the text itself when it does not parse. */
export function bjtTime(iso: string): string {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t + BJT_OFFSET_MS).toISOString().slice(0, 16).replace('T', ' ');
}

/** The opening of a new-ticker research chat started from the candidate list; the user edits it before sending. */
export function candidateDraft(c: Candidate): string {
  const lines = [`调研 ${candidateLabel(c)}。`, '', 'KOL 线索：'];
  for (const s of c.sources) {
    lines.push(`- @${s.handle}（${STANCE_LABEL[s.stance]}，${bjtTime(s.posted_at)} 北京时间）：${s.reason}`);
    lines.push(`  原文：${s.quote}`);
    lines.push(`  原帖：${s.url}`);
  }
  return lines.join('\n');
}

/** A Grok chat's model as picked: model, context window, effort (grok-4.6-500k · high). */
export function discoverModelText(
  conv: Pick<ChatSummary, 'model' | 'effort' | 'context_window' | 'lookback_days'>,
): string {
  const window = conv.context_window ? `-${Math.round(conv.context_window / 1000)}k` : '';
  return `${conv.model}${window} · ${conv.effort}${conv.lookback_days ? ` · ${conv.lookback_days}天` : ''}`;
}

export function costText(usd: number | null | undefined): string {
  return usd == null ? '' : `本对话 $${usd.toFixed(2)}`;
}

/** Streamed text up to the candidate block: the block reads well only as cards, once the reply is complete. */
export function partialText(partial: string): { text: string; collecting: boolean } {
  const at = partial.indexOf(BLOCK_START);
  return at < 0 ? { text: partial, collecting: false } : { text: partial.slice(0, at).trimEnd(), collecting: true };
}
