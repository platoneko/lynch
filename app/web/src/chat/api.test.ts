import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backoffMs, createChat, editMessage, openChatEvents, STALE_MS } from './api';
import { getConnection, resetConnection } from '../offline/connection';
import type { ChatList } from './types';

const listed: ChatList = {
  chats: [{ id: 'c1', kind: 'research', ticker: null, title: 't', model: 'claude-opus-5-5', effort: 'medium',
    created_at: 1, updated_at: 1, context_window: 1_000_000, archived: false, state: 'idle',
    agent: 'claude', agent_label: 'Claude Code', model_label: 'Opus 5.5' }],
  running_count: 0, usage: null,
};

describe('input identifiers on every entry point', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('sends the same local identifier on creation and editing', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(Response.json({ id: 'created' }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetch);
    await createChat({ kind: 'research', ticker: null, text: 'first', model: 'm', effort: 'medium',
      agent: 'codex', client_id: 'first-client-id' });
    await editMessage('created', 'user-old', 'edited', 'edit-client-id');
    expect(JSON.parse(fetch.mock.calls[0][1].body).client_id).toBe('first-client-id');
    expect(JSON.parse(fetch.mock.calls[1][1].body).client_id).toBe('edit-client-id');
  });
});

describe('backoffMs', () => {
  it('doubles from one second and caps at thirty', () => {
    expect([0, 1, 2, 3, 4, 5, 6, 20].map(backoffMs)).toEqual([1000, 2000, 4000, 8000, 16_000, 30_000, 30_000, 30_000]);
  });
});

/** An EventSource whose frames the test delivers by hand. */
class FakeSource {
  static readonly CLOSED = 2;
  static opened: FakeSource[] = [];
  readyState = 1;
  private listeners = new Map<string, ((m: MessageEvent) => void)[]>();
  constructor(readonly url: string) {
    FakeSource.opened.push(this);
  }
  addEventListener(type: string, fn: (m: MessageEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  close(): void {
    this.readyState = FakeSource.CLOSED;
  }
  emit(type: string, data: unknown = {}): void {
    for (const fn of this.listeners.get(type) ?? []) fn({ data: JSON.stringify(data) } as MessageEvent);
  }
}

describe('openChatEvents', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSource.opened = [];
    vi.stubGlobal('EventSource', FakeSource);
    vi.stubGlobal('fetch', vi.fn(async () => Response.json(listed)));
    resetConnection();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const handlers = () => ({ snapshot: vi.fn(), event: vi.fn(), reset: vi.fn(), gone: vi.fn(), lost: vi.fn() });

  it('reopens a stream that went silent: the connection died without an error', async () => {
    const on = handlers();
    const stop = openChatEvents('c1', on);
    const [first] = FakeSource.opened;
    first.emit('snapshot', { items: [] });
    await vi.advanceTimersByTimeAsync(STALE_MS - 1000);
    first.emit('ping');
    await vi.advanceTimersByTimeAsync(STALE_MS - 1000);
    expect(on.lost).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1000);
    expect(on.lost).toHaveBeenCalledOnce();
    expect(first.readyState).toBe(FakeSource.CLOSED);
    await vi.advanceTimersByTimeAsync(backoffMs(0));
    expect(FakeSource.opened).toHaveLength(2);
    stop();
  });

  it('stops watching once closed', async () => {
    const on = handlers();
    openChatEvents('c1', on)();
    const [first] = FakeSource.opened;
    first.emit('snapshot', { items: ['late'] });
    first.emit('state', { state: 'idle' });
    first.emit('reset');
    first.emit('archived');
    first.emit('error');
    await vi.advanceTimersByTimeAsync(STALE_MS * 2);
    expect(on.lost).not.toHaveBeenCalled();
    expect(on.snapshot).not.toHaveBeenCalled();
    expect(on.event).not.toHaveBeenCalled();
    expect(on.reset).not.toHaveBeenCalled();
    expect(on.gone).not.toHaveBeenCalled();
    expect(FakeSource.opened).toHaveLength(1);
  });

  it('reopens within fifteen seconds even when the list request never answers', async () => {
    let requestSignal: AbortSignal | undefined;
    vi.stubGlobal('fetch', (_path: string, init: RequestInit) => {
      requestSignal = init.signal ?? undefined;
      return new Promise<Response>(() => {});
    });
    const on = handlers();
    const stop = openChatEvents('c1', on);
    await vi.advanceTimersByTimeAsync(STALE_MS);
    expect(on.lost).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(15_000);
    expect(FakeSource.opened).toHaveLength(2);
    expect(requestSignal?.aborted).toBe(true);
    expect(on.gone).not.toHaveBeenCalled();
    stop();
  });

  it('also bounds recovery when headers arrive but the list body hangs', async () => {
    vi.stubGlobal('fetch', async () => new Response(new ReadableStream()));
    const on = handlers();
    const stop = openChatEvents('c1', on);
    await vi.advanceTimersByTimeAsync(STALE_MS + 15_000);
    expect(FakeSource.opened).toHaveLength(2);
    expect(on.gone).not.toHaveBeenCalled();
    stop();
  });

  it('aborts a list request on close and ignores its late result', async () => {
    let requestSignal: AbortSignal | undefined;
    let answer!: (response: Response) => void;
    vi.stubGlobal('fetch', (_path: string, init: RequestInit) => {
      requestSignal = init.signal ?? undefined;
      return new Promise<Response>((resolve) => { answer = resolve; });
    });
    const on = handlers();
    const stop = openChatEvents('c1', on);
    await vi.advanceTimersByTimeAsync(STALE_MS);
    stop();
    expect(requestSignal?.aborted).toBe(true);
    answer(Response.json({ ...listed, chats: [] }));
    await vi.advanceTimersByTimeAsync(STALE_MS * 2);
    expect(FakeSource.opened).toHaveLength(1);
    expect(on.gone).not.toHaveBeenCalled();
  });

  it('keeps the service online when fetch rejects an intentional abort', async () => {
    let requestSignal: AbortSignal | undefined;
    vi.stubGlobal('fetch', (_path: string, init: RequestInit) => {
      requestSignal = init.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        requestSignal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
      });
    });
    const on = handlers();
    const stop = openChatEvents('c1', on);
    await vi.advanceTimersByTimeAsync(STALE_MS);
    stop();
    await vi.advanceTimersByTimeAsync(STALE_MS * 2);
    expect(requestSignal?.aborted).toBe(true);
    expect(getConnection()).toEqual({ offline: false });
    expect(on.gone).not.toHaveBeenCalled();
    expect(FakeSource.opened).toHaveLength(1);
  });

  it('cancels a scheduled reopen when closed during the backoff', async () => {
    const on = handlers();
    const stop = openChatEvents('c1', on);
    await vi.advanceTimersByTimeAsync(STALE_MS);
    stop();
    await vi.advanceTimersByTimeAsync(STALE_MS * 2);
    expect(FakeSource.opened).toHaveLength(1);
    expect(on.gone).not.toHaveBeenCalled();
  });

  it('ignores repeated errors and late events from an old connection during recovery', async () => {
    let answer!: (response: Response) => void;
    vi.stubGlobal('fetch', () => new Promise<Response>((resolve) => { answer = resolve; }));
    const on = handlers();
    const stop = openChatEvents('c1', on);
    const [first] = FakeSource.opened;
    await vi.advanceTimersByTimeAsync(STALE_MS);
    first.emit('error');
    expect(on.lost).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(15_000);
    expect(FakeSource.opened).toHaveLength(2);
    const current = FakeSource.opened[1];
    current.emit('snapshot', { items: [] });
    answer(Response.json({ ...listed, chats: [] }));
    first.emit('snapshot', { items: ['old'] });
    first.emit('state', { state: 'dormant' });
    first.emit('reset');
    first.emit('archived');
    first.emit('error');
    await vi.advanceTimersByTimeAsync(2000);
    expect(FakeSource.opened).toHaveLength(2);
    expect(current.readyState).toBe(1);
    expect(on.gone).not.toHaveBeenCalled();
    expect(on.snapshot).toHaveBeenCalledExactlyOnceWith({ items: [] });
    expect(on.event).not.toHaveBeenCalled();
    expect(on.reset).not.toHaveBeenCalled();
    stop();
  });

  it('reports gone without reopening when the server no longer lists the chat', async () => {
    vi.stubGlobal('fetch', async () => Response.json({ ...listed, chats: [] }));
    const on = handlers();
    const stop = openChatEvents('c1', on);
    await vi.advanceTimersByTimeAsync(STALE_MS + 2000);
    expect(on.gone).toHaveBeenCalledOnce();
    expect(FakeSource.opened).toHaveLength(1);
    stop();
  });

  it('reopens after a list request fails instead of treating a server error as gone', async () => {
    vi.stubGlobal('fetch', async () => new Response('unavailable', { status: 503 }));
    const on = handlers();
    const stop = openChatEvents('c1', on);
    FakeSource.opened[0].close();
    FakeSource.opened[0].emit('error');
    await vi.advanceTimersByTimeAsync(2000);
    expect(FakeSource.opened).toHaveLength(2);
    expect(on.gone).not.toHaveBeenCalled();
    stop();
  });
});
