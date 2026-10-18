/** Test stand-in for CacheStorage: entries keyed by URL path, as the service worker and sync key them. */
const ORIGIN = 'http://127.0.0.1:31510';

function pathOf(key: RequestInfo | URL): string {
  if (typeof key === 'string') return new URL(key, ORIGIN).pathname;
  return new URL(key instanceof URL ? key : key.url).pathname;
}

export class FakeCache {
  readonly entries = new Map<string, Response>();

  async match(key: RequestInfo | URL): Promise<Response | undefined> {
    return this.entries.get(pathOf(key))?.clone();
  }

  async put(key: RequestInfo | URL, res: Response): Promise<void> {
    this.entries.set(pathOf(key), res);
  }

  async delete(key: RequestInfo | URL): Promise<boolean> {
    return this.entries.delete(pathOf(key));
  }

  async keys(): Promise<{ url: string }[]> {
    return [...this.entries.keys()].map((p) => ({ url: ORIGIN + p }));
  }
}

export class FakeCaches {
  readonly byName = new Map<string, FakeCache>();

  async open(name: string): Promise<FakeCache> {
    if (!this.byName.has(name)) this.byName.set(name, new FakeCache());
    return this.byName.get(name)!;
  }
}

export const FAKE_ORIGIN = ORIGIN;
