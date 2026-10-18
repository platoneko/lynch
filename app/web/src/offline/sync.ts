// Keeps the phone's copies (public/sw.js answers from them offline) in step while the server answers:
// the app shell from the build's precache.json, and every framework / axis doc from the server's manifest.
const SHELL = 'lynch-shell';
const DATA = 'lynch-data';
const MIN_GAP_MS = 10 * 60_000; // the docs change rarely; a sync per visit to the list would be wasted traffic

export type SyncDeps = { caches: CacheStorage; fetch: typeof fetch; now: () => Date };

const live = (): SyncDeps => ({ caches, fetch: (...a) => fetch(...a), now: () => new Date() });

let running = false;
let lastRun = 0;

/** Called after each online read of the home list; a failed run is retried by the next call. */
export function syncOffline(): void {
  if (typeof caches === 'undefined') return; // no service worker here either: nothing to keep
  if (running || Date.now() - lastRun < MIN_GAP_MS) return;
  running = true;
  const d = live();
  Promise.all([syncShell(d), syncDocs(d)])
    .then(() => { lastRun = Date.now(); }, () => {})
    .finally(() => { running = false; });
}

/**
 * Every hashed build file, so a doc with math still has its KaTeX fonts offline; files of older builds go.
 * The kept page is replaced in the same run: it must name this build's files, or after a redeploy an offline
 * cold start would load a page whose scripts were just pruned (the worker also keeps "/" on each online open).
 */
export async function syncShell(d: SyncDeps = live()): Promise<void> {
  const wanted: string[] = await readJson(d, '/precache.json');
  const shell = await d.caches.open(SHELL);
  for (const path of wanted) {
    if (await shell.match(path)) continue;
    const res = await d.fetch(path);
    if (res.ok) await shell.put(path, res);
  }
  const page = await d.fetch('/', { cache: 'no-store' });
  if (!page.ok) throw new Error(`/: ${page.status}`); // keep the old page and the old files together
  await shell.put('/', page);
  const keep = new Set(wanted);
  for (const path of await cachedPaths(shell)) {
    if (path.startsWith('/assets/') && !keep.has(path)) await shell.delete(path);
  }
}

/** Every pool doc, archived ones included: fetch only those whose tag differs, drop those gone from the pool. */
export async function syncDocs(d: SyncDeps = live()): Promise<void> {
  const manifest: { frameworks: Record<string, string>; axes: Record<string, string> } =
    await readJson(d, '/api/offline-manifest');
  const tags = new Map<string, string>([
    ...Object.entries(manifest.frameworks).map(([t, tag]) => [`/api/frameworks/${encodeURIComponent(t)}`, tag] as const),
    ...Object.entries(manifest.axes).map(([n, tag]) => [`/api/axes/${encodeURIComponent(n)}`, tag] as const),
  ]);
  const data = await d.caches.open(DATA);
  // One at a time: a first sync is ~100 docs, and the phone's link may be thin.
  for (const [path, tag] of tags) {
    if ((await data.match(path))?.headers.get('ETag') === tag) continue;
    const res = await d.fetch(path, { cache: 'no-store' });
    // An offline-stamped answer is the worker's own copy, not the server's: keep what is held.
    if (!res.ok || res.headers.has('X-Lynch-Offline')) continue;
    const headers = new Headers(res.headers);
    headers.set('X-Lynch-Saved-At', d.now().toISOString());
    await data.put(path, new Response(await res.arrayBuffer(), { status: 200, headers }));
  }
  for (const path of await cachedPaths(data)) {
    const isDoc = path.startsWith('/api/frameworks/') || path.startsWith('/api/axes/');
    if (isDoc && !tags.has(path)) await data.delete(path);
  }
}

async function readJson<T>(d: SyncDeps, path: string): Promise<T> {
  const res = await d.fetch(path, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json() as Promise<T>;
}

async function cachedPaths(cache: Cache): Promise<string[]> {
  return (await cache.keys()).map((r) => new URL(r.url).pathname);
}
