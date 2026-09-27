import { describe, expect, it } from 'vitest';
import {
  applyEvent, compactText, contextText, contextWarn, editableUuids, formatTokens, fromSnapshot, toChatEvent,
  toolDetail, toolSummary, turnDocLinks,
} from './chatModel';
import type { ChatItem, ChatSummary, Snapshot, ToolItem } from './types';

const conv: ChatSummary = {
  id: 'c1', kind: 'ticker', ticker: 'NVDA.US', title: 't', model: 'claude-opus-5-5', effort: 'medium',
  created_at: 1, updated_at: 1, context_window: 1_000_000, archived: false, state: 'idle',
};
const user = (uuid: string, text = uuid): ChatItem => ({ kind: 'user', key: uuid, uuid, text });
const reply = (key: string, text = key): ChatItem => ({ kind: 'text', key, text });
const tool = (id: string, name: string, input: Record<string, unknown>, output: string | null = 'ok'): ToolItem =>
  ({ kind: 'tool', key: id, id, name, input, output, is_error: false, denied: false });
const snap = (items: ChatItem[]): Snapshot =>
  ({ conv, items, partial: '', usage: null, context: { tokens: 41_000, window: 1_000_000 } });

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
    const v = { ...fromSnapshot(snap([])), queued: [{ localId: 'l1', text: 'more' }, { localId: 'l2', text: 'x' }] };
    expect(applyEvent(v, { type: 'item', item: user('u9', 'more') }).queued).toEqual([{ localId: 'l2', text: 'x' }]);
  });
  it('does not let a folded duplicate settle a queued echo', () => {
    const v = { ...fromSnapshot(snap([user('u1', 'more')])), queued: [{ localId: 'l1', text: 'more' }] };
    expect(applyEvent(v, { type: 'item', item: user('u1', 'more') }).queued).toEqual([{ localId: 'l1', text: 'more' }]);
  });
  it('settles the same text queued twice one arrival at a time', () => {
    let v = { ...fromSnapshot(snap([])), queued: [{ localId: 'l1', text: 'go' }, { localId: 'l2', text: 'go' }] };
    v = applyEvent(v, { type: 'item', item: user('u1', 'go') });
    expect(v.queued).toEqual([{ localId: 'l2', text: 'go' }]);
    v = applyEvent(v, { type: 'item', item: user('u2', 'go') });
    expect(v.queued).toEqual([]);
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
  const queued = [{ localId: 'l1', text: 'A' }, { localId: 'l2', text: 'B' }];
  it('settles an echo consumed while the stream was down', () => {
    const prev = { ...fromSnapshot(snap([user('u1')])), queued };
    expect(fromSnapshot(snap([user('u1'), user('u2', 'A')]), prev).queued).toEqual([{ localId: 'l2', text: 'B' }]);
  });
  it('leaves echoes alone for messages the view already had', () => {
    const prev = { ...fromSnapshot(snap([user('u1', 'A')])), queued };
    expect(fromSnapshot(snap([user('u1', 'A')]), prev).queued).toEqual(queued);
  });
  it('starts with nothing queued', () => {
    expect(fromSnapshot(snap([user('u1', 'A')])).queued).toEqual([]);
  });
});

describe('toChatEvent', () => {
  it('wraps each SSE payload', () => {
    expect(toChatEvent('delta', { text: 'a' })).toEqual({ type: 'delta', text: 'a' });
    expect(toChatEvent('state', { state: 'running' })).toEqual({ type: 'state', state: 'running' });
    expect(toChatEvent('context', { tokens: 1, window: 2 })).toEqual({ type: 'context', context: { tokens: 1, window: 2 } });
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
