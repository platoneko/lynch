import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backoffMs, openChatEvents, STALE_MS } from './api';

vi.mock('../api', () => ({
  get: vi.fn(async () => ({ json: async () => ({ chats: [{ id: 'c1' }] }) })),
  post: vi.fn(),
}));

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
    await vi.advanceTimersByTimeAsync(STALE_MS * 2);
    expect(on.lost).not.toHaveBeenCalled();
    expect(FakeSource.opened).toHaveLength(1);
  });
});
