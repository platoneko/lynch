import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { CompactDialog } from './CompactDialog';
import { applyEvent, canCompact, fromSnapshot, limitsText, toChatEvent } from './chatModel';
import type { ChatAgent, Snapshot } from './types';

const snapshot: Snapshot = {
  conv: { id: 'c1', kind: 'ticker', ticker: 'AAA.US', title: 't', model: 'model', effort: 'medium',
    created_at: 1, updated_at: 1, context_window: 1000, archived: false, state: 'running',
    agent: 'codex', agent_label: 'Codex', model_label: 'model' },
  items: [], partial: '', retry: null, usage: null, context: { tokens: null, window: 1000 }, approvals: [],
};

describe('shared agent interactions', () => {
  it('shows actual quota windows and distinguishes missing data from zero usage', () => {
    expect(limitsText(null)).toBe('额度未提供');
    expect(limitsText({ rateLimits: { primary: { usedPercent: 0, windowDurationMins: 300 },
      secondary: { usedPercent: 42, windowDurationMins: 10080 } } })).toBe('5小时 0% · 7天 42%');
    expect(limitsText({ rateLimits: { primary: { usedPercent: 27 } } })).toBe('本期 27%');
  });
  it.each<ChatAgent>(['claude', 'codex', 'grok', 'opencode'])('offers real preservation instructions for %s', (agent) => {
    expect(canCompact({ agent })).toBe(true);
    const html = renderToStaticMarkup(createElement(CompactDialog, {
      agent, hint: '保留合同金额', onHint: () => {}, onClose: () => {}, onCompact: () => {},
    }));
    expect(html).toContain('<input');
    expect(html).toContain('保留合同金额');
  });

  it('restores durable queued messages and accepts queue updates', () => {
    let view = fromSnapshot({ ...snapshot, queued: [{ id: 'q1', text: 'next', status: 'behind' }] });
    expect(view.queued).toMatchObject([{ localId: 'q1', text: 'next', status: 'behind' }]);
    const event = toChatEvent('queued', { queued: [{ id: 'q1', text: 'next', status: 'uncertain' }] });
    expect(event).not.toBeNull();
    view = applyEvent(view, event!);
    expect(view.queued).toMatchObject([{ localId: 'q1', status: 'uncertain' }]);
  });

  it('merges the server queue with an in-flight local send without duplicate bubbles', () => {
    const local = { ...fromSnapshot(snapshot), queued: [{ localId: 'q1', text: 'next', status: 'behind' as const }] };
    const view = fromSnapshot({ ...snapshot, queued: [{ id: 'q1', text: 'next', status: 'behind' }] }, local);
    expect(view.queued).toHaveLength(1);
  });

  it('does not show an acknowledged waiting echo again after reconnect', () => {
    const view = fromSnapshot({ ...snapshot,
      items: [{ kind: 'user', key: 'u1', uuid: 'u1', text: 'next', client_id: 'q1' }],
      queued: [{ id: 'q1', text: 'next', status: 'waiting' }],
    });
    expect(view.queued).toEqual([]);
  });

  it('does not settle a local send using an identical echo from another device', () => {
    const local = { ...fromSnapshot(snapshot), queued: [{ localId: 'local-id', text: '继续', status: 'waiting' as const }] };
    const view = applyEvent(local, { type: 'item', item: {
      kind: 'user', key: 'remote-user', uuid: 'remote-user', text: '继续', client_id: 'remote-id',
    } });
    expect(view.queued).toHaveLength(1);
    expect(view.queued[0].localId).toBe('local-id');
  });

  it.each(['first-client-id', 'edit-client-id'])('settles the %s handoff on snapshot and SSE', (clientId) => {
    const pending = [{ localId: clientId, text: 'input', status: 'waiting' as const }];
    const item = { kind: 'user' as const, key: 'u1', uuid: 'u1', text: 'input', client_id: clientId };
    expect(fromSnapshot({ ...snapshot, items: [item], queued: [] }, null, pending).queued).toEqual([]);
    const view = applyEvent({ ...fromSnapshot(snapshot), queued: pending }, { type: 'item', item });
    expect(applyEvent(view, { type: 'state', state: 'idle' }).queued).toEqual([]);
  });
});
