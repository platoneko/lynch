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

export async function get(path: string): Promise<Response> {
  const res = await send(path, { cache: 'no-store' });
  pairIfUnpaired(res);
  // The server explains a failure in the body; surface it verbatim.
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 300)}`);
  return res;
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

/** `refresh`: a tap on refresh, which lets the server refetch quotes sooner than on a plain load. */
export async function fetchCatalog(refresh: boolean): Promise<Catalog> {
  return (await get(refresh ? '/api/tickers?refresh=1' : '/api/tickers')).json();
}

export async function fetchFramework(ticker: string): Promise<string> {
  return (await get(`/api/frameworks/${encodeURIComponent(ticker)}`)).text();
}

export async function fetchAxis(name: string): Promise<string> {
  return (await get(`/api/axes/${encodeURIComponent(name)}`)).text();
}

/** Hide the ticker from the home list; its document and eval script stay. */
export async function archiveFramework(ticker: string): Promise<void> {
  await post(`/api/frameworks/${encodeURIComponent(ticker)}/archive`);
}

export async function restoreFramework(ticker: string): Promise<void> {
  await post(`/api/frameworks/${encodeURIComponent(ticker)}/restore`);
}
