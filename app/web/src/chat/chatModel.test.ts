import { describe, expect, it } from 'vitest';
import {
  applyEvent, compactText, contextText, contextWarn, diffLineClass, editableUuids, formatTokens, fromSnapshot,
  inputJumps, toChatEvent, toolDetail, toolSummary, turnDocLinks, type Queued,
} from './chatModel';
import type { Approval, ChatItem, ChatSummary, Snapshot, ToolItem } from './types';

const conv: ChatSummary = {
  id: 'c1', kind: 'ticker', ticker: 'NVDA.US', title: 't', model: 'claude-opus-5-5', effort: 'medium',
  created_at: 1, updated_at: 1, context_window: 1_000_000, archived: false, state: 'idle',
};
const user = (uuid: string, text = uuid): ChatItem => ({ kind: 'user', key: uuid, uuid, text });
const reply = (key: string, text = key): ChatItem => ({ kind: 'text', key, text });
const tool = (id: string, name: string, input: Record<string, unknown>, output: string | null = 'ok'): ToolItem =>
  ({ kind: 'tool', key: id, id, name, input, output, is_error: false, denied: false });
const pending = (localId: string, text: string, status: Queued['status'] = 'waiting'): Queued => ({ localId, text, status });
const snap = (items: ChatItem[]): Snapshot =>
  ({ conv, items, partial: '', retry: null, usage: null, context: { tokens: 41_000, window: 1_000_000 }, approvals: [] });

describe('applyEvent', () => {
  it('folds a duplicate item by key instead of appending it', () => {
    const v = applyEvent(fromSnapshot(snap([user('u1'), reply('a1#0')])), { type: 'item', item: reply('a1#0') });
    expect(v.items).toHaveLength(2);
  });
  it('streams deltas and clears them on the full text', () => {
    let v = fromSnapshot(snap([]));
    v = applyEvent(v, { type: 'delta', text: 'ec' });
    v = applyEvent(v, { type: 'delta', text: 'ho' });
    expect(v.partial).toBe('echo');
    v = applyEvent(v, { type: 'item', item: reply('a#0', 'echo') });
    expect(v.partial).toBe('');
  });
  it('patches a tool with its result', () => {
    const v = applyEvent(fromSnapshot(snap([tool('t1', 'Read', {}, null)])),
      { type: 'tool_result', result: { kind: 'tool_result', id: 't1', output: 'body', is_error: true, denied: true } });
    expect(v.items[0]).toMatchObject({ output: 'body', is_error: true, denied: true });
  });
  it('turns a queued echo into the real message', () => {
    const v = { ...fromSnapshot(snap([])), queued: [pending('l1', 'more'), pending('l2', 'x')] };
    expect(applyEvent(v, { type: 'item', item: user('u9', 'more') }).queued).toEqual([pending('l2', 'x')]);
  });
  it('does not let a folded duplicate settle a queued echo', () => {
    const v = { ...fromSnapshot(snap([user('u1', 'more')])), queued: [pending('l1', 'more')] };
    expect(applyEvent(v, { type: 'item', item: user('u1', 'more') }).queued).toEqual([pending('l1', 'more')]);
  });
  it('settles the same text queued twice one arrival at a time', () => {
    let v = { ...fromSnapshot(snap([])), queued: [pending('l1', 'go'), pending('l2', 'go')] };
    v = applyEvent(v, { type: 'item', item: user('u1', 'go') });
    expect(v.queued).toEqual([pending('l2', 'go')]);
    v = applyEvent(v, { type: 'item', item: user('u2', 'go') });
    expect(v.queued).toEqual([]);
  });
  it('settles an unanswered message whose echo comes late', () => {
    const v = { ...fromSnapshot(snap([])), queued: [pending('l1', 'go', 'unanswered')] };
    expect(applyEvent(v, { type: 'item', item: user('u1', 'go') }).queued).toEqual([]);
  });
  it('marks a message unanswered when its turn ends without its echo', () => {
    const v = { ...fromSnapshot(snap([])), conv: { ...conv, state: 'running' as const }, queued: [pending('l1', 'hi')] };
    expect(applyEvent(v, { type: 'state', state: 'idle' }).queued).toEqual([pending('l1', 'hi', 'unanswered')]);
    expect(applyEvent(v, { type: 'state', state: 'dormant' }).queued).toEqual([pending('l1', 'hi', 'unanswered')]);
  });
  it('drops an unechoed slash command when its turn ends: claude never replays one', () => {
    const v = { ...fromSnapshot(snap([])), conv: { ...conv, state: 'running' as const }, queued: [pending('l1', '/compact')] };
    expect(applyEvent(v, { type: 'state', state: 'idle' }).queued).toEqual([]);
  });
  it('moves a message sent behind a turn up to waiting when that turn ends', () => {
    const v = { ...fromSnapshot(snap([])), conv: { ...conv, state: 'running' as const }, queued: [pending('l1', 'next', 'behind')] };
    expect(applyEvent(v, { type: 'state', state: 'idle' }).queued).toEqual([pending('l1', 'next')]);
  });
  it('leaves a waiting message alone while a dormant chat wakes up', () => {
    const v = { ...fromSnapshot(snap([])), conv: { ...conv, state: 'dormant' as const }, queued: [pending('l1', 'hi')] };
    expect(applyEvent(v, { type: 'state', state: 'idle' }).queued).toEqual([pending('l1', 'hi')]);
  });
  it('shows an API retry until the server clears it', () => {
    let v = applyEvent(fromSnapshot(snap([])), { type: 'retry', text: '接口 181 秒没有响应' });
    expect(v.retry).toBe('接口 181 秒没有响应');
    v = applyEvent(v, { type: 'retry', text: null });
    expect(v.retry).toBeNull();
    expect(fromSnapshot({ ...snap([]), retry: 'r' }).retry).toBe('r');
  });
  it('drops the partial when the turn ends', () => {
    const v = applyEvent({ ...fromSnapshot(snap([])), partial: 'half' }, { type: 'state', state: 'idle' });
    expect(v.partial).toBe('');
    expect(v.conv.state).toBe('idle');
  });
  it('keeps the partial while compacting and drops it when dormant', () => {
    const v = { ...fromSnapshot(snap([])), partial: 'half' };
    expect(applyEvent(v, { type: 'state', state: 'compacting' }).partial).toBe('half');
    expect(applyEvent(v, { type: 'state', state: 'dormant' }).partial).toBe('');
  });
});

describe('fromSnapshot', () => {
  const queued = [pending('l1', 'A'), pending('l2', 'B')];
  it('settles an echo consumed while the stream was down', () => {
    const prev = { ...fromSnapshot(snap([user('u1')])), queued };
    expect(fromSnapshot(snap([user('u1'), user('u2', 'A')]), prev).queued).toEqual([pending('l2', 'B')]);
  });
  it('marks a message unanswered when its turn ended while the stream was down', () => {
    const prev = { ...fromSnapshot(snap([user('u1')])), conv: { ...conv, state: 'running' as const }, queued };
    expect(fromSnapshot(snap([user('u1'), user('u2', 'A')]), prev).queued).toEqual([pending('l2', 'B', 'unanswered')]);
  });
  it('leaves echoes alone for messages the view already had', () => {
    const prev = { ...fromSnapshot(snap([user('u1', 'A')])), queued };
    expect(fromSnapshot(snap([user('u1', 'A')]), prev).queued).toEqual(queued);
  });
  it('starts with nothing queued', () => {
    expect(fromSnapshot(snap([user('u1', 'A')])).queued).toEqual([]);
  });
  it('starts a new chat with its first message pending until the transcript has it', () => {
    const first = [pending('l1', 'A')];
    expect(fromSnapshot(snap([]), null, first).queued).toEqual(first);
    expect(fromSnapshot(snap([user('u1', 'A')]), null, first).queued).toEqual([]);
  });
  it("keeps an edit's new text pending across the cut branch's snapshot until claude replays it", () => {
    const edited = [pending('l1', 'B2')];
    const prev = { ...fromSnapshot(snap([user('u1', 'A'), reply('a1#0'), user('u2', 'B'), reply('a2#0')])), queued: edited };
    const cut = fromSnapshot({ ...snap([user('u1', 'A'), reply('a1#0')]), conv: { ...conv, state: 'running' } }, prev);
    expect(cut.queued).toEqual(edited);
    expect(applyEvent(cut, { type: 'item', item: user('u3', 'B2') }).queued).toEqual([]);
  });
});

describe('toChatEvent', () => {
  it('wraps each SSE payload', () => {
    expect(toChatEvent('delta', { text: 'a' })).toEqual({ type: 'delta', text: 'a' });
    expect(toChatEvent('state', { state: 'running' })).toEqual({ type: 'state', state: 'running' });
    expect(toChatEvent('context', { tokens: 1, window: 2 })).toEqual({ type: 'context', context: { tokens: 1, window: 2 } });
    expect(toChatEvent('retry', { text: '接口出错' })).toEqual({ type: 'retry', text: '接口出错' });
    expect(toChatEvent('retry', { text: null })).toEqual({ type: 'retry', text: null });
    expect(toChatEvent('nope', {})).toBeNull();
  });
});

describe('editableUuids', () => {
  it('skips the first message and anything before the latest compaction', () => {
    const items: ChatItem[] = [
      user('u1'), reply('a1'), user('u2'), reply('a2'),
      { kind: 'compact', key: 'b', trigger: 'manual', pre_tokens: 1, post_tokens: 1 },
      user('u3'), reply('a3'), user('u4'),
    ];
    expect([...editableUuids(items)]).toEqual(['u4']);
  });
  it('allows a later message once a reply exists', () => {
    expect([...editableUuids([user('u1'), tool('t', 'Read', {}), user('u2')])]).toEqual(['u2']);
  });
});

describe('tool text', () => {
  it('summarises common tools with a status mark', () => {
    expect(toolSummary(tool('1', 'Read', { file_path: '/home/neko/plat1ko/lynch/frameworks/NVDA.US.md' })))
      .toBe('读取 frameworks/NVDA.US.md ✓');
    expect(toolSummary(tool('2', 'Edit', { file_path: 'frameworks/NVDA.US.md', old_string: 'a\nb', new_string: 'c\nd\ne\nf' })))
      .toBe('修改 frameworks/NVDA.US.md +4 −2 ✓');
    expect(toolSummary(tool('3', 'Bash', { command: 'cd frameworks && just check' }, null))).toBe('运行 cd frameworks && just check …');
    expect(toolSummary({ ...tool('4', 'Bash', { command: 'git -C frameworks push' }), is_error: true, denied: true }))
      .toBe('运行 git -C frameworks push · 已拒绝');
    expect(toolSummary(tool('5', 'mcp__wisburg__list-articles', {}))).toBe('wisburg · list-articles ✓');
  });
  it('shows an edit as a diff', () => {
    expect(toolDetail(tool('1', 'Edit', { file_path: 'f', old_string: 'a', new_string: 'b' }))).toBe('- a\n+ b');
  });
});

describe('turnDocLinks', () => {
  it('links each turn to the frameworks it wrote, after its last item', () => {
    const items: ChatItem[] = [
      user('u1'), tool('t1', 'Write', { file_path: 'frameworks/NEW.US.md' }),
      tool('t2', 'Edit', { file_path: '/x/lynch/frameworks/_TEMPLATE.md' }), reply('a1'),
      user('u2'), reply('a2'),
    ];
    expect([...turnDocLinks(items)]).toEqual([[3, ['NEW.US']]]);
  });
});

describe('formatting', () => {
  it('formats tokens and context', () => {
    expect(formatTokens(950)).toBe('950');
    expect(formatTokens(41_450)).toBe('41k');
    expect(formatTokens(1_000_000)).toBe('1M');
    expect(contextText({ tokens: 41_450, window: 1_000_000 })).toBe('41k/1M');
    expect(contextText({ tokens: null, window: 1_000_000 })).toBe('—/1M');
    expect(contextText({ tokens: 41_450, window: null })).toBe('41k/—');
    expect(contextWarn({ tokens: 710_000, window: 1_000_000 })).toBe(true);
    expect(contextWarn({ tokens: null, window: 1_000_000 })).toBe(false);
  });
  it('describes a compaction', () => {
    expect(compactText({ kind: 'compact', key: 'b', trigger: 'auto', pre_tokens: 41_454, post_tokens: 2879 }))
      .toBe('已压缩（自动）：41k → 3k');
  });
});

describe('inputJumps', () => {
  // The visible band runs from the header's bottom (100) to the composer's top (700).
  it('leads to the nearest input above the band and to an off-screen last input', () => {
    expect(inputJumps([-900, -300, 50, 1200], 100, 700)).toEqual({ prev: 2, last: 'below' });
  });

  it('skips the input a jump just parked at the band top, so repeated taps walk upward', () => {
    expect(inputJumps([-300, 108, 900], 100, 700)).toEqual({ prev: 0, last: 'below' });
  });

  it('points up to the last input once the reader has followed the reply past it', () => {
    expect(inputJumps([-900, -300], 100, 700)).toEqual({ prev: 1, last: 'above' });
  });

  it('offers nothing while the last input is on screen and none is above', () => {
    expect(inputJumps([300], 100, 700)).toEqual({ prev: null, last: null });
    expect(inputJumps([], 100, 700)).toEqual({ prev: null, last: null });
  });
});

const approval = (request_id: string): Approval =>
  ({ request_id, tool_use_id: `t-${request_id}`, tool: 'Edit', path: 'frameworks/.claude/skills/sediment-framework/r.md', diff: '-a\n+b' });

describe('approvals', () => {
  it('come with the snapshot so a reconnect shows them again', () => {
    expect(fromSnapshot({ ...snap([]), approvals: [approval('r1')] }).approvals.map((a) => a.request_id)).toEqual(['r1']);
  });
  it('are added once and removed when answered', () => {
    let v = fromSnapshot(snap([]));
    v = applyEvent(v, { type: 'approval', approval: approval('r1') });
    v = applyEvent(v, { type: 'approval', approval: approval('r1') });
    v = applyEvent(v, { type: 'approval', approval: approval('r2') });
    expect(v.approvals.map((a) => a.request_id)).toEqual(['r1', 'r2']);
    v = applyEvent(v, { type: 'approval_done', request_id: 'r1' });
    expect(v.approvals.map((a) => a.request_id)).toEqual(['r2']);
  });
  it('parse from SSE frames', () => {
    expect(toChatEvent('approval', approval('r1'))).toEqual({ type: 'approval', approval: approval('r1') });
    expect(toChatEvent('approval_done', { request_id: 'r1' })).toEqual({ type: 'approval_done', request_id: 'r1' });
  });
});

describe('diffLineClass', () => {
  it('colours added, removed and hunk lines but not the file headers', () => {
    expect(['+++ b/x', '--- a/x', '@@ -1 +1 @@', '+new', '-old', ' same', '（内容没有变化）'].map(diffLineClass))
      .toEqual(['diff-meta', 'diff-meta', 'diff-hunk', 'diff-add', 'diff-del', '', '']);
  });
});
