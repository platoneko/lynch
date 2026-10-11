import type { Catalog } from './types';

/** 401 = this device was never paired or was revoked; the server's pairing page is the only way back in. */
function pairIfUnpaired(res: Response): void {
  if (res.status === 401) window.location.assign('/pair');
}

/** fetch() rejects with a TypeError only when nothing answered: on the phone, tailfwd's lynch forward is down. */
async function send(path: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(path, init);
  } catch (e) {
    if (e instanceof TypeError) throw new Error('连不上 lynch 服务，请确认 tailfwd 在运行');
    throw e;
  }
}

async function answered(res: Response): Promise<Response> {
  pairIfUnpaired(res);
  // The server explains a failure in the body; surface it verbatim.
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 300)}`);
  return res;
}

export async function get(path: string): Promise<Response> {
  return answered(await send(path, { cache: 'no-store' }));
}

/** A server answer and its tag (ETag), held to ask next time whether the answer changed. */
export interface Tagged<T> {
  value: T;
  etag: string | null;
}

/**
 * GET unless the answer still equals the one held: `held` is that answer's ETag, and null comes back when
 * the server says unchanged (an empty 304).
 */
export async function getIfChanged(path: string, held: string | null): Promise<Response | null> {
  const res = await send(path, { cache: 'no-store', headers: held === null ? {} : { 'If-None-Match': held } });
  return res.status === 304 ? null : answered(res);
}

async function tagged<T>(path: string, held: string | null, read: (res: Response) => Promise<T>): Promise<Tagged<T> | null> {
  const res = await getIfChanged(path, held);
  return res && { value: await read(res), etag: res.headers.get('ETag') };
}

/**
 * A JSON POST; the server explains a refusal in `{"error": ...}`, surfaced verbatim. An empty
 * explanation falls back to the status code so the UI never shows a blank error.
 * Always JSON, even without a body: the server refuses other content types (its CSRF guard).
 */
export async function post(path: string, body: unknown = {}): Promise<Response> {
  const res = await send(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  pairIfUnpaired(res);
  if (!res.ok) {
    const text = await res.text();
    let reason = text.slice(0, 300);
    try { reason = (JSON.parse(text) as { error?: string }).error ?? reason; } catch { /* not JSON */ }
    throw new Error(reason || String(res.status));
  }
  return res;
}

/** The frameworks as last edited beside the server's latest polled quotes; null while unchanged since `held`. */
export async function fetchCatalog(held: string | null): Promise<Tagged<Catalog> | null> {
  return tagged('/api/tickers', held, (res) => res.json());
}

export async function fetchFramework(ticker: string, held: string | null): Promise<Tagged<string> | null> {
  return tagged(`/api/frameworks/${encodeURIComponent(ticker)}`, held, (res) => res.text());
}

export async function fetchAxis(name: string, held: string | null): Promise<Tagged<string> | null> {
  return tagged(`/api/axes/${encodeURIComponent(name)}`, held, (res) => res.text());
}

/** Hide the ticker from the home list; its document and eval script stay. */
export async function archiveFramework(ticker: string): Promise<void> {
  await post(`/api/frameworks/${encodeURIComponent(ticker)}/archive`);
}

export async function restoreFramework(ticker: string): Promise<void> {
  await post(`/api/frameworks/${encodeURIComponent(ticker)}/restore`);
}
