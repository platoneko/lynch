import { afterEach, describe, expect, it, vi } from 'vitest';
import { FakeCaches } from './fakeCaches';
import { syncDocs, syncOffline, syncShell, type SyncDeps } from './sync';

function deps(routes: Record<string, () => Response>) {
  const caches = new FakeCaches();
  const fetched: string[] = [];
  const fetchFake = async (path: RequestInfo | URL) => {
    const p = String(path);
    fetched.push(p);
    const route = routes[p];
    return route ? route() : new Response('missing', { status: 404 });
  };
  const d: SyncDeps = {
    caches: caches as unknown as CacheStorage,
    fetch: fetchFake as typeof fetch,
    now: () => new Date('2026-10-18T12:00:00Z'),
  };
  return { caches, fetched, d };
}

const doc = (body: string, tag: string) => () => new Response(body, { headers: { ETag: tag } });
const json = (v: unknown) => () => new Response(JSON.stringify(v));

describe('syncDocs', () => {
  it('fetches only changed and new docs', async () => {
    const { caches, fetched, d } = deps({
      '/api/offline-manifest': json({ frameworks: { 'AAA.US': '"a2"', 'BBB.US': '"b1"' }, axes: { rates: '"r1"' } }),
      '/api/frameworks/AAA.US': doc('# AAA v2', '"a2"'),
      '/api/axes/rates': doc('# rates', '"r1"'),
    });
    const data = await caches.open('lynch-data');
    await data.put('/api/frameworks/AAA.US', new Response('# AAA v1', { headers: { ETag: '"a1"' } }));
    await data.put('/api/frameworks/BBB.US', new Response('# BBB', { headers: { ETag: '"b1"' } }));
    await syncDocs(d);
    expect(fetched.filter((p) => p !== '/api/offline-manifest')).toEqual(['/api/frameworks/AAA.US', '/api/axes/rates']);
    const aaa = (await data.match('/api/frameworks/AAA.US'))!;
    expect(await aaa.text()).toBe('# AAA v2');
    expect(aaa.headers.get('X-Lynch-Saved-At')).toBe('2026-10-18T12:00:00.000Z');
  });

  it('drops docs gone from the manifest', async () => {
    const { caches, d } = deps({ '/api/offline-manifest': json({ frameworks: {}, axes: {} }) });
    const data = await caches.open('lynch-data');
    await data.put('/api/frameworks/OLD.US', new Response('# old', { headers: { ETag: '"o"' } }));
    await data.put('/api/tickers', new Response('{}'));
    await syncDocs(d);
    expect([...data.entries.keys()]).toEqual(['/api/tickers']);
  });
});

describe('syncShell', () => {
  it('adds missing assets, replaces / with the same build, and prunes old ones', async () => {
    const { caches, fetched, d } = deps({
      '/precache.json': json(['/assets/new.js', '/assets/kept.css']),
      '/assets/new.js': () => new Response('js'),
      '/': () => new Response('<html v2>'),
    });
    const shell = await caches.open('lynch-shell');
    await shell.put('/', new Response('<html>'));
    await shell.put('/assets/kept.css', new Response('css'));
    await shell.put('/assets/old.js', new Response('old'));
    await syncShell(d);
    expect(fetched).toEqual(['/precache.json', '/assets/new.js', '/']);
    expect([...shell.entries.keys()].sort()).toEqual(['/', '/assets/kept.css', '/assets/new.js']);
    // The kept page must name this build's files, or an offline cold start loads assets just pruned.
    expect(await (await shell.match('/'))!.text()).toBe('<html v2>');
  });
});

describe('syncOffline', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('does nothing without caches', () => {
    const spy = vi.fn();
    vi.stubGlobal('fetch', spy);
    syncOffline();
    expect(spy).not.toHaveBeenCalled();
  });
});
