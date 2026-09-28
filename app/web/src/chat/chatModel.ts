import type {
  ChatEvent, ChatItem, ChatState, ChatSummary, ContextUsage, Snapshot, ToolItem, ToolResult, Usage,
} from './types';

/** A message sent while the agent runs, shown until the server replays it. */
export interface Queued {
  localId: string;
  text: string;
}

export interface ChatView {
  conv: ChatSummary;
  items: ChatItem[];
  partial: string; // streamed text of the reply being written
  usage: Usage | null;
  context: ContextUsage;
  queued: Queued[];
}

export function isBusy(state: ChatState): boolean {
  return state === 'running' || state === 'compacting';
}

/**
 * A (re)connect's snapshot as the view. A queued message consumed while the stream was down shows up only
 * here, never as a live `item`, so each user message new to this view settles one queued echo.
 */
export function fromSnapshot(s: Snapshot, prev: ChatView | null = null): ChatView {
  const known = new Set(prev?.items.map((i) => i.key));
  let queued = prev?.queued ?? [];
  for (const it of s.items) {
    if (it.kind === 'user' && !known.has(it.key)) queued = dropEcho(queued, it.text);
  }
  return { conv: s.conv, items: s.items, partial: s.partial, usage: s.usage, context: s.context, queued };
}

/** Removes the first queued message with this text: one arrival settles one echo. */
function dropEcho(queued: Queued[], text: string): Queued[] {
  const at = queued.findIndex((q) => q.text === text);
  return at >= 0 ? queued.filter((_, n) => n !== at) : queued;
}

/** An SSE frame as a typed event; unknown frames are ignored. */
export function toChatEvent(type: string, data: unknown): ChatEvent | null {
  const d = data as Record<string, unknown>;
  switch (type) {
    case 'delta': return { type, text: String(d.text) };
    case 'item': return { type, item: data as ChatItem };
    case 'tool_result': return { type, result: data as ToolResult };
    case 'state': return { type, state: d.state as ChatState };
    case 'usage': return { type, usage: data as Usage };
    case 'context': return { type, context: data as ContextUsage };
    case 'conv': return { type, conv: data as ChatSummary };
    default: return null;
  }
}

export function applyEvent(v: ChatView, e: ChatEvent): ChatView {
  switch (e.type) {
    case 'delta':
      return { ...v, partial: v.partial + e.text };
    case 'item': {
      // A snapshot and the live stream may both carry an item; its key makes the second a no-op.
      const at = v.items.findIndex((i) => i.key === e.item.key);
      const items = at >= 0 ? v.items.map((i, n) => (n === at ? e.item : i)) : [...v.items, e.item];
      const partial = e.item.kind === 'text' ? '' : v.partial;
      // Only a first arrival settles an echo; a folded duplicate was already counted.
      const queued = e.item.kind === 'user' && at < 0 ? dropEcho(v.queued, e.item.text) : v.queued;
      return { ...v, items, partial, queued };
    }
    case 'tool_result': {
      const r = e.result;
      const items = v.items.map((i) =>
        i.kind === 'tool' && i.id === r.id ? { ...i, output: r.output, is_error: r.is_error, denied: r.denied } : i);
      return { ...v, items };
    }
    case 'state':
      return { ...v, conv: { ...v.conv, state: e.state }, partial: isBusy(e.state) ? v.partial : '' };
    case 'usage':
      return { ...v, usage: e.usage };
    case 'context':
      return { ...v, context: e.context };
    case 'conv':
      return { ...v, conv: { ...e.conv, state: v.conv.state } };
  }
}

/**
 * User messages the server will accept an edit for (spec §3g): after the latest compaction, and with a
 * reply before them on that side of it — the reply is where the conversation gets cut.
 */
export function editableUuids(items: ChatItem[]): Set<string> {
  const lastCompact = items.map((i) => i.kind).lastIndexOf('compact');
  const out = new Set<string>();
  let replied = false;
  items.forEach((it, n) => {
    if (n <= lastCompact) return;
    if (it.kind === 'text' || it.kind === 'tool') replied = true;
    if (it.kind === 'user' && replied) out.add(it.uuid);
  });
  return out;
}

const VERBS: Record<string, string> = {
  Read: '读取', Write: '写入', Edit: '修改', Grep: '搜索', Glob: '查找文件',
  WebSearch: '搜索网页', WebFetch: '抓取', Skill: '使用 skill', Bash: '运行',
};

export function toolSummary(t: ToolItem): string {
  const status = t.output === null ? ' …' : t.denied ? ' · 已拒绝' : t.is_error ? ' ✗' : ' ✓';
  return `${toolAction(t)}${status}`;
}

function toolAction(t: ToolItem): string {
  const mcp = /^mcp__([^_]+(?:_[^_]+)*)__(.+)$/.exec(t.name);
  if (mcp) return `${mcp[1]} · ${mcp[2]}`;
  const input = t.input;
  const verb = VERBS[t.name] ?? t.name;
  switch (t.name) {
    case 'Read':
    case 'Write':
      return `${verb} ${repoPath(input.file_path)}`;
    case 'Edit':
      return `${verb} ${repoPath(input.file_path)} +${lines(input.new_string)} −${lines(input.old_string)}`;
    case 'Bash':
      return `${verb} ${String(input.command ?? '').split('\n')[0].slice(0, 80)}`;
    case 'Grep':
    case 'Glob':
      return `${verb} ${String(input.pattern ?? '')}`;
    case 'WebSearch':
      return `${verb} ${String(input.query ?? '')}`;
    case 'WebFetch':
      return `${verb} ${String(input.url ?? '')}`;
    case 'Skill':
      return `${verb} ${String(input.skill ?? input.name ?? '')}`;
    default:
      return verb;
  }
}

export function toolDetail(t: ToolItem): string {
  if (t.name === 'Edit') {
    const side = (mark: string, s: unknown) => String(s ?? '').split('\n').map((l) => `${mark} ${l}`).join('\n');
    return `${side('-', t.input.old_string)}\n${side('+', t.input.new_string)}`;
  }
  return JSON.stringify(t.input, null, 2);
}

/** A path as seen from the lynch repo: absolute paths lose everything before `frameworks/` (or the repo). */
function repoPath(p: unknown): string {
  const s = String(p ?? '');
  const at = s.indexOf('frameworks/');
  if (at >= 0) return s.slice(at);
  const repo = s.indexOf('/lynch/');
  return repo >= 0 ? s.slice(repo + '/lynch/'.length) : s;
}

function lines(s: unknown): number {
  return String(s ?? '').split('\n').length;
}

const FRAMEWORK = /(?:^|\/)frameworks\/([^/]+)\.md$/;

/** For each turn that wrote a framework, the index of its last item → the tickers to link after it. */
export function turnDocLinks(items: ChatItem[]): Map<number, string[]> {
  const out = new Map<number, string[]>();
  let touched: string[] = [];
  items.forEach((it, n) => {
    if (it.kind === 'tool' && (it.name === 'Write' || it.name === 'Edit')) {
      const m = FRAMEWORK.exec(String(it.input.file_path ?? ''));
      const ticker = m?.[1];
      if (ticker && !ticker.startsWith('_') && ticker !== 'AGENTS' && ticker !== 'README' && !touched.includes(ticker)) {
        touched = [...touched, ticker];
      }
    }
    const turnEnds = n === items.length - 1 || items[n + 1].kind === 'user';
    if (turnEnds && touched.length) {
      out.set(n, touched);
      touched = [];
    }
  });
  return out;
}

export type InputJumps = { prev: number | null; last: 'above' | 'below' | null };

/**
 * Where the input-jump buttons lead, given each user bubble's top in viewport px (in page order)
 * and the band [bandTop, bandBottom) the header and composer leave visible.
 * `prev` is the nearest bubble starting above the band; `last` says which way the last bubble lies, if off screen.
 */
export function inputJumps(tops: number[], bandTop: number, bandBottom: number): InputJumps {
  let prev: number | null = null;
  tops.forEach((top, i) => {
    if (top < bandTop) prev = i;
  });
  const lastTop = tops.at(-1);
  const last = lastTop === undefined ? null : lastTop < bandTop ? 'above' : lastTop >= bandBottom ? 'below' : null;
  return { prev, last };
}

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

export function contextText(c: ContextUsage): string {
  const used = c.tokens === null ? '—' : formatTokens(c.tokens);
  return `${used}/${c.window ? formatTokens(c.window) : '—'}`;
}

export function contextWarn(c: ContextUsage): boolean {
  return c.tokens !== null && !!c.window && c.tokens / c.window > 0.7;
}

export function usageText(u: Usage | null): string {
  if (!u) return '';
  const pct = (x: number | null) => (x === null ? '—' : `${Math.round(x * 100)}%`);
  return `5h ${pct(u.five_hour)} · 7d ${pct(u.seven_day)}`;
}

export function compactText(it: Extract<ChatItem, { kind: 'compact' }>): string {
  const n = (x: number | null) => (x === null ? '?' : formatTokens(x));
  return `已压缩${it.trigger === 'auto' ? '（自动）' : ''}：${n(it.pre_tokens)} → ${n(it.post_tokens)}`;
}

export function stateText(state: ChatState): string {
  return { running: '运行中', compacting: '压缩中', idle: '空闲', dormant: '已休眠（下次发送自动恢复）' }[state];
}
