export type Route =
  | { page: 'home' }
  | { page: 'doc'; ticker: string }
  | { page: 'axis'; name: string }
  | { page: 'chats' }
  | { page: 'chat'; id: string }
  | { page: 'chat-new'; ticker: string | null };

export function parseRoute(hash: string): Route {
  const doc = /^#\/doc\/([^/?#]+)$/.exec(hash);
  if (doc) return { page: 'doc', ticker: decodeURIComponent(doc[1]) };
  const axis = /^#\/axis\/([^/?#]+)$/.exec(hash);
  if (axis) return { page: 'axis', name: decodeURIComponent(axis[1]) };
  if (hash === '#/chats') return { page: 'chats' };
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
