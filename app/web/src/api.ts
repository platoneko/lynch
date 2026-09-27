import type { Catalog } from './types';

export async function get(path: string): Promise<Response> {
  const res = await fetch(path, { cache: 'no-store' });
  // The phone proxy explains a dead server in the body; surface it verbatim.
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 300)}`);
  return res;
}

/**
 * A JSON POST; the server explains a refusal in `{"error": ...}`, surfaced verbatim. An empty
 * explanation falls back to the status code so the UI never shows a blank error.
 * Always JSON, even without a body: the server refuses other content types (its CSRF guard).
 */
export async function post(path: string, body: unknown = {}): Promise<Response> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
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
