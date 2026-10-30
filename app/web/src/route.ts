export type Route =
  | { page: 'home' }
  | { page: 'doc'; ticker: string }
  | { page: 'axis'; name: string }
  | { page: 'chats' }
  | { page: 'chat'; id: string }
  | { page: 'chat-new'; ticker: string | null }
  | { page: 'chat-new-discover' }
  | { page: 'candidates' }
  | { page: 'earnings' }
  | { page: 'anomalies' }
  | { page: 'watch' };

export function parseRoute(hash: string): Route {
  const doc = /^#\/doc\/([^/?#]+)$/.exec(hash);
  if (doc) return { page: 'doc', ticker: decodeURIComponent(doc[1]) };
  const axis = /^#\/axis\/([^/?#]+)$/.exec(hash);
  if (axis) return { page: 'axis', name: decodeURIComponent(axis[1]) };
  if (hash === '#/chats') return { page: 'chats' };
  if (hash === '#/candidates') return { page: 'candidates' };
  if (hash === '#/earnings') return { page: 'earnings' };
  if (hash === '#/anomalies') return { page: 'anomalies' };
  if (hash === '#/watch') return { page: 'watch' };
  // Checked before chat ids: `new-discover` would otherwise read as one (ids are uuids).
  if (hash === '#/chat/new-discover') return { page: 'chat-new-discover' };
  // `new` is not a conversation id: ids are uuids.
  const fresh = /^#\/chat\/new(?:\/([^/?#]+))?$/.exec(hash);
  if (fresh) return { page: 'chat-new', ticker: fresh[1] ? decodeURIComponent(fresh[1]) : null };
  const chat = /^#\/chat\/([^/?#]+)$/.exec(hash);
  if (chat) return { page: 'chat', id: decodeURIComponent(chat[1]) };
  return { page: 'home' };
}

export function docHash(ticker: string): string {
  return `#/doc/${encodeURIComponent(ticker)}`;
}

export function axisHash(name: string): string {
  return `#/axis/${encodeURIComponent(name)}`;
}

export function chatsHash(): string {
  return '#/chats';
}

export function chatHash(id: string): string {
  return `#/chat/${encodeURIComponent(id)}`;
}

export function newChatHash(ticker: string | null): string {
  return ticker ? `#/chat/new/${encodeURIComponent(ticker)}` : '#/chat/new';
}

export function newDiscoverHash(): string {
  return '#/chat/new-discover';
}

export function candidatesHash(): string {
  return '#/candidates';
}

export function earningsHash(): string {
  return '#/earnings';
}

export function anomaliesHash(): string {
  return '#/anomalies';
}

export function watchHash(): string {
  return '#/watch';
}
