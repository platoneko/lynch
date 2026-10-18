import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FAKE_ORIGIN, FakeCaches } from './fakeCaches';

const SOURCE = readFileSync(new URL('../../public/sw.js', import.meta.url), 'utf-8');

type FakeRequest = { method: string; url: string; mode: string };
type FetchEvent = { request: FakeRequest; respondWith: (r: Promise<Response>) => void; waitUntil: (p: Promise<unknown>) => void };
type Handler = (event: FetchEvent) => void;

/** Runs public/sw.js against a fake worker scope; `request` returns its answer, or null when it let the request pass. */
function worker(fetchImpl: (req: FakeRequest, init?: { signal?: AbortSignal }) => Promise<Response>) {
  const messages: unknown[] = [];
  const listeners = new Map<string, Handler>();
  const caches = new FakeCaches();
  const scope = {
    location: new URL(FAKE_ORIGIN + '/sw.js'),
    addEventListener: (type: string, fn: Handler) => listeners.set(type, fn),
    skipWaiting: () => Promise.resolve(),
    clients: {
      claim: () => Promise.resolve(),
      matchAll: async () => [{ postMessage: (m: unknown) => messages.push(m) }],
    },
  };
  new Function('self', 'caches', 'fetch', SOURCE)(scope, caches, fetchImpl);
  const request = (path: string, init: Partial<FakeRequest> = {}): Promise<Response> | null => {
    let answer: Promise<Response> | null = null;
    listeners.get('fetch')!({
      request: { method: 'GET', mode: 'cors', url: FAKE_ORIGIN + path, ...init },
      respondWith: (r) => { answer = r; },
      waitUntil: () => {},
    });
    return answer;
  };
  return { caches, request, messages };
}

const html = () => new Response('<!doctype html><title>lynch</title>', { headers: { 'Content-Type': 'text/html' } });
const never = () => new Promise<Response>(() => {});

afterEach(() => vi.useRealTimers());

describe('service worker', () => {
  it('navigation falls back to the cached shell when fetch never resolves', async () => {
    let online = true;
    const w = worker(() => (online ? Promise.resolve(html()) : never()));
    await w.request('/', { mode: 'navigate' });
    online = false;
    vi.useFakeTimers();
    const answer = w.request('/', { mode: 'navigate' })!;
    await vi.advanceTimersByTimeAsync(4000);
    expect(await (await answer).text()).toContain('<title>lynch</title>');
  });

  it('navigation passes a 302 to /pair through and does not cache it', async () => {
    const w = worker(() => Promise.resolve(new Response(null, { status: 302, headers: { Location: '/pair' } })));
    expect((await w.request('/', { mode: 'navigate' })!).status).toBe(302);
    expect((await w.caches.open('lynch-shell')).entries.size).toBe(0);
  });

  it('api read caches a 200 and serves it marked offline when the network fails', async () => {
    let online = true;
    const w = worker(() => (online
      ? Promise.resolve(new Response('# AAA', { headers: { ETag: '"t1"' } }))
      : Promise.reject(new TypeError('Failed to fetch'))));
    const first = await w.request('/api/frameworks/AAA.US')!;
    expect(first.headers.get('X-Lynch-Offline')).toBeNull();
    online = false;
    const cached = await w.request('/api/frameworks/AAA.US')!;
    expect(cached.headers.get('X-Lynch-Offline')).toBe('1');
    expect(cached.headers.get('ETag')).toBe('"t1"');
    expect(cached.headers.get('X-Lynch-Saved-At')).not.toBeNull();
    expect(await cached.text()).toBe('# AAA');
  });

  it('api 401 passes through and is not served from cache', async () => {
    let status = 200;
    const w = worker(() => Promise.resolve(new Response('{}', { status })));
    await w.request('/api/tickers');
    status = 401;
    expect((await w.request('/api/tickers')!).status).toBe(401);
  });

  it('a late answer after the fallback still refreshes the copy and announces online', async () => {
    vi.useFakeTimers();
    let body = 'v1';
    const w = worker(() => new Promise((resolve) => setTimeout(() => resolve(new Response(body)), body === 'v1' ? 0 : 6000)));
    const first = w.request('/api/tickers')!;
    await vi.advanceTimersByTimeAsync(0);
    await first;
    body = 'v2';
    const answer = w.request('/api/tickers')!;
    await vi.advanceTimersByTimeAsync(4000);
    expect(await (await answer).text()).toBe('v1');
    await vi.advanceTimersByTimeAsync(2000);
    expect(await (await w.caches.open('lynch-data')).match('/api/tickers').then((r) => r!.text())).toBe('v2');
    expect(w.messages).toEqual(['lynch-online']);
  });

  it('a hung request is aborted after 20 s', async () => {
    vi.useFakeTimers();
    let signal: AbortSignal | undefined;
    const w = worker((_req, init) => { signal = init?.signal; return never(); });
    void w.request('/api/tickers');
    await vi.advanceTimersByTimeAsync(19_999);
    expect(signal!.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(signal!.aborted).toBe(true);
  });

  it('a POST and /api/chats are not intercepted', () => {
    const w = worker(() => Promise.resolve(html()));
    expect(w.request('/api/frameworks/AAA.US/archive', { method: 'POST' })).toBeNull();
    expect(w.request('/api/chats')).toBeNull();
  });
});
