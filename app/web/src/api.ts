import type { Catalog } from './types';

async function get(path: string): Promise<Response> {
  const res = await fetch(path, { cache: 'no-store' });
  // The phone proxy explains a dead server in the body; surface it verbatim.
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 300)}`);
  return res;
}

export async function fetchCatalog(): Promise<Catalog> {
  return (await get('/api/tickers')).json();
}

export async function fetchFramework(ticker: string): Promise<string> {
  return (await get(`/api/frameworks/${encodeURIComponent(ticker)}`)).text();
}
