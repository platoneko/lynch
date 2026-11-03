import { costText, discoverModelText } from './candidateModel';
import type {
  Approval, ChatAgent, ChatEvent, ChatItem, ChatKind, ChatOptions, ChatState, ChatSummary, ContextUsage,
  GrokResearchOptions, Snapshot, ToolItem, ToolResult, Usage,
} from './types';

/**
 * A sent message, shown until the server replays it. Claude replays a message only once the model starts
 * answering it, which a slow or failing API can put off for minutes.
 */
export interface Queued {
  localId: string;
  text: string;
  // behind: sent during another turn; waiting: its turn is the current or next one;
  // unanswered: its turn ended without replaying it, so it was probably never answered
  status: 'behind' | 'waiting' | 'unanswered';
}

export interface ChatView {
  conv: ChatSummary;
  items: ChatItem[];
  partial: string; // streamed text of the reply being written
  retry: string | null;
  usage: Usage | null;
  context: ContextUsage;
  queued: Queued[];
  approvals: Approval[]; // skill edits waiting for 允许 / 拒绝
}

export function isBusy(state: ChatState): boolean {
  return state === 'running' || state === 'compacting';
}

/** Only Claude Code chats edit messages and count against the Claude subscription. */
export function isClaude(c: Pick<ChatSummary, 'agent'>): boolean {
  return c.agent === 'claude';
}

export function canCompact(c: Pick<ChatSummary, 'agent'>): boolean {
  return c.agent === 'claude' || c.agent === 'codex';
}

/** Which model list the blank 研究 / 讨论 picker shows. Grok never falls through to Claude's models. */
export function researchPickerAgent(agent: ChatAgent): ChatAgent {
  return agent;
}

/**
 * The Grok model / window / effort the blank page should send. An empty or Claude id (the first paint,
 * before state catches up) maps onto Grok's default, not Opus.
 */
export function grokSelection(
  g: GrokResearchOptions, modelId: string, effort = '', ctxWindow = 0,
): { model: string; effort: string; ctxWindow: number } | null {
  const m = g.models.find((x) => x.id === modelId)
    ?? g.models.find((x) => x.id === g.default_model)
    ?? g.models[0];
  if (!m) return null;
  return {
    model: m.id,
    effort: m.efforts.includes(effort) ? effort
      : (m.efforts.includes(g.default_effort) ? g.default_effort : m.default_effort),
    ctxWindow: m.windows.includes(ctxWindow) ? ctxWindow
      : (m.windows.includes(g.default_window) ? g.default_window : m.windows[0]),
  };
}

/** Research / ticker choices; Codex advertises effort levels separately for each model. */
export function researchSettings(options: ChatOptions, agent: ChatAgent, model = '', effort = '', ctxWindow = 0) {
  if (agent === 'grok') {
    const picked = grokSelection(options.grok, model, effort, ctxWindow);
    const selected = options.grok.models.find((m) => m.id === picked?.model);
    return {
      models: options.grok.models, model: picked?.model ?? '', effort: picked?.effort ?? '',
      efforts: selected?.efforts ?? [], windows: selected?.windows ?? [], ctxWindow: picked?.ctxWindow ?? 0,
      hint: options.grok.hint ?? null,
    };
  }
  const source = agent === 'codex' ? options.codex : agent === 'opencode' ? options.opencode : options;
  const models = source.models;
  const selected = models.find((m) => m.id === model)
    ?? models.find((m) => m.id === source.default_model) ?? models[0];
  const codexModel = agent === 'codex' ? options.codex.models.find((m) => m.id === selected?.id) : null;
  const efforts = agent === 'codex' ? codexModel?.efforts ?? [] : agent === 'opencode' ? [] : options.efforts;
  const defaultEffort = agent === 'codex' ? codexModel?.default_effort ?? ''
    : agent === 'opencode' ? 'default' : options.default_effort;
  return {
    models, model: selected?.id ?? '', efforts, windows: [] as number[], ctxWindow: 0,
    effort: efforts.includes(effort) ? effort : defaultEffort,
    hint: agent === 'codex' ? options.codex.hint ?? (!selected ? '暂时无法获取 Codex 模型，请检查本机登录状态' : null) : null,
  };
}

/** Reject stale model / effort state as well as a missing Codex model list. */
export function canStartResearch(options: ChatOptions | null, agent: ChatAgent, model: string, effort: string): boolean {
  if (!options) return false;
  if (agent === 'grok') return grokSelection(options.grok, model, effort) !== null;
  if (!model) return false;
  const selected = researchSettings(options, agent, model, effort);
  return selected.model === model && selected.effort === effort;
}

/** Subscription agents do not expose per-chat USD pricing. */
export function chatCostText(c: ChatSummary): string {
  if (c.agent === 'grok' || (c.agent === 'opencode' && c.cost_usd)) return costText(c.cost_usd);
  return '';
}

/** "default": the model has no effort levels to pick from (opencode's). */
export function effortText(effort: string): string {
  return effort === 'default' ? '默认' : effort;
}

/** What runs a chat, as its header and list row show it: agent · model · effort. */
export function agentModelText(c: ChatSummary): string {
  if (c.agent === 'grok') return `${c.agent_label} · ${discoverModelText(c)}`;
  return `${c.agent_label} · ${c.model_label} · ${effortText(c.effort)}`;
}

/** POST /api/chats body for a blank-page first message. */
export function newChatBody(args: {
  discover: boolean; kind: ChatKind; ticker: string | null; text: string; model: string; effort: string;
  agent: ChatAgent; ctxWindow: number; lookbackDays: number;
}): {
  kind: ChatKind; ticker: string | null; text: string; model: string; effort: string;
  context_window?: number; lookback_days?: number; agent?: ChatAgent;
} {
  const { discover, kind, ticker, text, model, effort, agent, ctxWindow, lookbackDays } = args;
  if (discover) {
    return { kind, ticker: null, text, model, effort, context_window: ctxWindow, lookback_days: lookbackDays };
  }
  return agent === 'grok'
    ? { kind, ticker, text, model, effort, agent, context_window: ctxWindow }
    : { kind, ticker, text, model, effort, agent };
}

/**
 * A (re)connect's snapshot as the view. A queued message consumed while the stream was down shows up only
 * here, never as a live `item`, so each user message new to this view settles one queued echo.
 * `sent` is what the first view starts with pending: a new chat's first message, sent from the blank page.
 */
export function fromSnapshot(s: Snapshot, prev: ChatView | null = null, sent: Queued[] = []): ChatView {
  const known = new Set(prev?.items.map((i) => i.key));
  let queued = prev?.queued ?? sent;
  for (const it of s.items) {
    if (it.kind === 'user' && !known.has(it.key)) queued = dropEcho(queued, it.text);
  }
  queued = settleQueued(queued, prev?.conv.state ?? null, s.conv.state);
  return {
    conv: s.conv, items: s.items, partial: s.partial, retry: s.retry, usage: s.usage, context: s.context, queued,
    approvals: s.approvals,
  };
}

/** Removes the first queued message with this text: one arrival settles one echo. */
function dropEcho(queued: Queued[], text: string): Queued[] {
  const at = queued.findIndex((q) => q.text === text);
  return at >= 0 ? queued.filter((_, n) => n !== at) : queued;
}

/**
 * A message waiting on a finished turn without a replay went unanswered (the process died, the API gave
 * up, the user stopped it), except a slash command, which claude runs itself and never replays.
 * A message sent behind the turn is next in line only while the process is still alive.
 */
function settleQueued(queued: Queued[], previous: ChatState | null, state: ChatState): Queued[] {
  if (isBusy(state)) return queued;
  // Repeated idle snapshots and dormant → idle during wake-up do not end another turn.
  if (state !== 'dormant' && previous !== null && !isBusy(previous)) return queued;
  return queued.flatMap((q): Queued[] => {
    if (q.status === 'behind') return [{ ...q, status: state === 'dormant' ? 'unanswered' : 'waiting' }];
    if (q.status === 'waiting') return q.text.startsWith('/') ? [] : [{ ...q, status: 'unanswered' }];
    return [q];
  });
}

/** An SSE frame as a typed event; unknown frames are ignored. */
export function toChatEvent(type: string, data: unknown): ChatEvent | null {
  const d = data as Record<string, unknown>;
  switch (type) {
    case 'delta': return { type, text: String(d.text) };
    case 'item': return { type, item: data as ChatItem };
    case 'tool_result': return { type, result: data as ToolResult };
    case 'state': return { type, state: d.state as ChatState };
    case 'retry': return { type, text: d.text === null ? null : String(d.text) };
    case 'usage': return { type, usage: data as Usage };
    case 'context': return { type, context: data as ContextUsage };
    case 'conv': return { type, conv: data as ChatSummary };
    case 'approval': return { type, approval: data as Approval };
    case 'approval_done': return { type, request_id: String(d.request_id) };
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
    case 'state': {
      const queued = settleQueued(v.queued, v.conv.state, e.state);
      return { ...v, conv: { ...v.conv, state: e.state }, partial: isBusy(e.state) ? v.partial : '', queued };
    }
    case 'retry':
      return { ...v, retry: e.text };
    case 'usage':
      return { ...v, usage: e.usage };
    case 'context':
      return { ...v, context: e.context };
    case 'conv':
      return { ...v, conv: { ...e.conv, state: v.conv.state } };
    case 'approval':
      // A reconnect's snapshot and the live event may both carry it.
      return v.approvals.some((a) => a.request_id === e.approval.request_id)
        ? v : { ...v, approvals: [...v.approvals, e.approval] };
    case 'approval_done':
      return { ...v, approvals: v.approvals.filter((a) => a.request_id !== e.request_id) };
  }
}

/** CSS class of one unified-diff line in an approval card. */
export function diffLineClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'diff-meta';
  if (line.startsWith('@@')) return 'diff-hunk';
  if (line.startsWith('+')) return 'diff-add';
  if (line.startsWith('-')) return 'diff-del';
  return '';
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
  // grok's server-side tools, named as its stream prints them
  'X search:': '搜索 X', 'Web search:': '搜索网页', web_fetch: '抓取',
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
      if (typeof input.move_path === 'string') {
        return `移动 ${repoPath(input.file_path)} → ${repoPath(input.move_path)}`;
      }
      if (typeof input.diff === 'string') return `${verb} ${repoPath(input.file_path)}`;
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
    if (typeof t.input.diff === 'string') return `${toolAction(t)}\n\n${t.input.diff}`;
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
    if (it.kind === 'tool' && (it.name === 'Write' || it.name === 'Edit')
      && it.output !== null && !it.is_error && !it.denied && it.input.operation !== 'delete') {
      const path = it.input.operation === 'move' ? it.input.move_path ?? it.input.file_path : it.input.file_path;
      const m = FRAMEWORK.exec(String(path ?? ''));
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
  const label = `已压缩${it.trigger === 'auto' ? '（自动）' : ''}`;
  if (it.pre_tokens === null || it.post_tokens === null) return label;
  return `${label}：${formatTokens(it.pre_tokens)} → ${formatTokens(it.post_tokens)}`;
}

export function stateText(state: ChatState): string {
  return { running: '运行中', compacting: '压缩中', idle: '空闲', dormant: '已休眠（下次发送自动恢复）' }[state];
}
