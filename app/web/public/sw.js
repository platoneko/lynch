// lynch offline: when the server does not answer, open the app from the phone's copy.
// The page keeps the copies in step while online (src/offline/sync.ts); this worker only answers from them.
// Navigation stays network-first: each page load renews the device cookie (auth/gate.py).
const SHELL = 'lynch-shell'; // index.html as "/", and the hashed /assets/ files
const DATA = 'lynch-data'; // the home list and every framework / axis doc, keyed by API path
const FALLBACK_MS = 4000; // a forward that accepts and then hangs: show the copy after this
// ...but keep waiting until this: a just-restarted server may hold the home list ~15 s on a quote fetch, and that
// late answer must still refresh the copy and bring the page back online. Then abort, so hung sockets do not pile up.
const GIVE_UP_MS = 20000;
const READS = /^\/api\/(tickers|frameworks\/[^/]+|axes\/[^/]+)$/;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

// Anything not matched here (chat streams, writes, earnings, ...) goes to the network untouched.
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === 'navigate' && url.pathname === '/') event.respondWith(networkOr(event, SHELL, '/', false));
  else if (url.pathname.startsWith('/assets/')) event.respondWith(cacheFirst(req, url.pathname));
  else if (READS.test(url.pathname)) event.respondWith(networkOr(event, DATA, url.pathname, true));
});

/** The server's answer, kept when it is a 200; any other answer (302 to /pair, 401, 304, 5xx) passes as is. */
async function networkOr(event, name, key, markOffline) {
  const answer = fetchWithin(event.request, GIVE_UP_MS).then(async (res) => {
    if (res.status === 200) await keep(name, key, res.clone());
    return res;
  });
  try {
    return await withTimeout(answer, FALLBACK_MS);
  } catch {
    event.waitUntil(answer.then(announceOnline, () => {}));
    return fromCache(name, key, markOffline);
  }
}

async function cacheFirst(req, key) {
  const hit = await (await caches.open(SHELL)).match(key);
  if (hit) return hit;
  const res = await fetchWithin(req, GIVE_UP_MS);
  if (res.status === 200) await keep(SHELL, key, res.clone());
  return res;
}

async function keep(name, key, res) {
  const headers = new Headers(res.headers);
  headers.set('X-Lynch-Saved-At', new Date().toISOString());
  await (await caches.open(name)).put(key, new Response(await res.arrayBuffer(), { status: 200, headers }));
}

/** The kept copy, stamped so the page knows it is offline; a network error when nothing was kept. */
async function fromCache(name, key, markOffline) {
  const hit = await (await caches.open(name)).match(key);
  if (!hit) return Response.error();
  if (!markOffline) return hit;
  const headers = new Headers(hit.headers);
  headers.set('X-Lynch-Offline', '1');
  return new Response(hit.body, { status: 200, headers });
}

/** The page shows the copy as offline until told the server answered after all (src/offline/connection.ts). */
async function announceOnline() {
  for (const client of await self.clients.matchAll()) client.postMessage('lynch-online');
}

function fetchWithin(req, ms) {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), ms);
  return fetch(req, { signal: abort.signal }).finally(() => clearTimeout(timer));
}

function withTimeout(p, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms);
    p.then((v) => { clearTimeout(timer); resolve(v); }, (e) => { clearTimeout(timer); reject(e); });
  });
}
