export type Route = { page: 'home' } | { page: 'doc'; ticker: string };

export function parseRoute(hash: string): Route {
  const m = /^#\/doc\/([^/?#]+)$/.exec(hash);
  return m ? { page: 'doc', ticker: decodeURIComponent(m[1]) } : { page: 'home' };
}

export function docHash(ticker: string): string {
  return `#/doc/${encodeURIComponent(ticker)}`;
}
