import { useSyncExternalStore } from 'react';

/**
 * Whether the server answered the last request. Offline, public/sw.js answers reads from the phone's copy and
 * stamps them `X-Lynch-Offline`; `savedAt` is when the home list being shown was saved (null: not known yet).
 */
export type Connection = { offline: false } | { offline: true; savedAt: string | null };

const ONLINE: Connection = { offline: false };
let state: Connection = ONLINE;
const listeners = new Set<() => void>();

function set(next: Connection): void {
  // Same value, same object: useSyncExternalStore re-renders on identity.
  if (next.offline === state.offline && (!next.offline || next.savedAt === (state as typeof next).savedAt)) return;
  state = next;
  listeners.forEach((l) => l());
}

const knownSavedAt = (): string | null => (state.offline ? state.savedAt : null);

/** Any answer from the server, 401 included, is online; only the worker's offline stamp is not. */
export function reportAnswer(res: Response): void {
  if (!res.headers.has('X-Lynch-Offline')) {
    set(ONLINE);
    return;
  }
  const isHomeList = res.url !== '' && new URL(res.url).pathname === '/api/tickers';
  set({ offline: true, savedAt: isHomeList ? res.headers.get('X-Lynch-Saved-At') : knownSavedAt() });
}

/** The worker saw the server answer after it had already shown the copy (public/sw.js announceOnline). */
export function reportOnline(): void {
  set(ONLINE);
}

/** Nothing answered and the phone had no copy either. */
export function reportUnreachable(): void {
  set({ offline: true, savedAt: knownSavedAt() });
}

export function getConnection(): Connection {
  return state;
}

export function useConnection(): Connection {
  return useSyncExternalStore((l) => {
    listeners.add(l);
    return () => listeners.delete(l);
  }, getConnection);
}

/** Tests only: back to the start-up state. */
export function resetConnection(): void {
  state = ONLINE;
}
