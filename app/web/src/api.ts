import type { Catalog } from './types';

async function get(path: string): Promise<Response> {
  const res = await fetch(path, { cache: 'no-store' });
  // The phone proxy explains a dead server in the body; surface it verbatim.
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 300)}`);
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
