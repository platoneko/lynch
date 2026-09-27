export type Route = { page: 'home' } | { page: 'doc'; ticker: string } | { page: 'axis'; name: string };

export function parseRoute(hash: string): Route {
  const doc = /^#\/doc\/([^/?#]+)$/.exec(hash);
  if (doc) return { page: 'doc', ticker: decodeURIComponent(doc[1]) };
  const axis = /^#\/axis\/([^/?#]+)$/.exec(hash);
  if (axis) return { page: 'axis', name: decodeURIComponent(axis[1]) };
  return { page: 'home' };
}

export function docHash(ticker: string): string {
  return `#/doc/${encodeURIComponent(ticker)}`;
}

export function axisHash(name: string): string {
  return `#/axis/${encodeURIComponent(name)}`;
}
