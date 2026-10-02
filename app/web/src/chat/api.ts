import { get, post } from '../api';
import { toChatEvent } from './chatModel';
import type { ChatEvent, ChatKind, ChatList, ChatOptions, SavedCandidate, Snapshot } from './types';

const base = (id: string) => `/api/chats/${encodeURIComponent(id)}`;

export async function fetchChats(): Promise<ChatList> {
  return (await get('/api/chats')).json();
}

export async function fetchChatOptions(): Promise<ChatOptions> {
  return (await get('/api/chats/options')).json();
}

export async function createChat(body: {
  kind: ChatKind; ticker: string | null; text: string; model: string; effort: string; context_window?: number;
  lookback_days?: number;
}): Promise<{ id: string }> {
  return (await post('/api/chats', body)).json();
}

export async function sendMessage(id: string, text: string): Promise<void> {
  await post(`${base(id)}/messages`, { text });
}

export async function interruptChat(id: string): Promise<void> {
  await post(`${base(id)}/interrupt`);
}

export async function editMessage(id: string, messageUuid: string, text: string): Promise<void> {
  await post(`${base(id)}/edit`, { message_uuid: messageUuid, text });
}

export async function archiveChat(id: string): Promise<void> {
  await post(`${base(id)}/archive`);
}

export async function renameChat(id: string, title: string): Promise<void> {
  await post(`${base(id)}/title`, { title });
}

export async function answerApproval(id: string, requestId: string, allow: boolean): Promise<void> {
  await post(`${base(id)}/approvals/${encodeURIComponent(requestId)}`, { allow });
}

export async function archiveTicker(ticker: string): Promise<{ archived: number }> {
  return (await post('/api/chats/archive', { ticker })).json();
}

/** Saves the ticked candidates of one Grok reply; the server reads them back from the transcript. */
export async function saveCandidates(id: string, key: string, tickers: string[]): Promise<{ saved: string[] }> {
  return (await post(`${base(id)}/candidates`, { key, tickers })).json();
}

export async function fetchCandidates(): Promise<SavedCandidate[]> {
  return ((await (await get('/api/candidates')).json()) as { candidates: SavedCandidate[] }).candidates;
}

export async function archiveCandidate(ticker: string): Promise<void> {
  await post(`/api/candidates/${encodeURIComponent(ticker)}/archive`);
}

export async function restoreCandidate(ticker: string): Promise<void> {
  await post(`/api/candidates/${encodeURIComponent(ticker)}/restore`);
}

const EVENTS = ['delta', 'item', 'tool_result', 'state', 'retry', 'usage', 'context', 'conv', 'approval', 'approval_done'];

/** Silence after which a stream counts as dead: three missed heartbeats (server chat/routes.py HEARTBEAT_S). */
export const STALE_MS = 45_000;

/** Delay before reopening a stream after `attempt` failed reopens: 1 s, 2 s, 4 s … capped at 30 s. */
export function backoffMs(attempt: number): number {
  return Math.min(1000 * 2 ** attempt, 30_000);
}

/**
 * Follows one conversation. Every (re)connect starts with a snapshot that replaces the view.
 * The browser retries a dropped network by itself (as when the server restarts behind the phone's forward,
 * which drops the connection); a non-200 answer (404 once the chat is archived or its transcript cleaned)
 * closes the EventSource for good,
 * and a connection that died silently (a frozen phone app, a changed tailnet path) never errors at all:
 * no frame for STALE_MS counts as the latter. Both are handled here: report `lost`, then `gone` if the chat
 * left the list, else reopen after a backoff. Returns a closer that also cancels a pending reopen.
 */
export function openChatEvents(id: string, on: {
  snapshot: (s: Snapshot) => void;
  event: (e: ChatEvent) => void;
  reset: () => void; // the branch changed (an edit): reopen for a fresh snapshot
  gone: () => void; // archived, or no longer listed
  lost: () => void; // the stream closed; a reopen is being tried
}): () => void {
  let es: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let watchdog: ReturnType<typeof setTimeout> | undefined;
  let attempt = 0;
  let stopped = false;

  function stop(): void {
    stopped = true;
    clearTimeout(timer);
    clearTimeout(watchdog);
    es?.close();
  }

  function connect(): void {
    const source = new EventSource(`${base(id)}/events`);
    es = source;
    const heard = () => {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => void recover(source), STALE_MS);
    };
    heard();
    source.addEventListener('ping', heard);
    source.addEventListener('snapshot', (m) => {
      heard();
      attempt = 0;
      on.snapshot(JSON.parse((m as MessageEvent).data));
    });
    for (const type of EVENTS) {
      source.addEventListener(type, (m) => {
        heard();
        const e = toChatEvent(type, JSON.parse((m as MessageEvent).data));
        if (e) on.event(e);
      });
    }
    source.addEventListener('reset', () => on.reset());
    source.addEventListener('archived', () => {
      stop();
      on.gone();
    });
    // CONNECTING means the browser is retrying on its own; CLOSED means it gave up.
    source.addEventListener('error', () => {
      if (source.readyState === EventSource.CLOSED) void recover(source);
    });
  }

  async function recover(source: EventSource): Promise<void> {
    clearTimeout(watchdog);
    source.close();
    on.lost();
    let listed = true; // the server being down is "try again later", not "gone"
    try {
      listed = (await fetchChats()).chats.some((c) => c.id === id);
    } catch { /* keep listed */ }
    if (stopped) return;
    if (!listed) {
      on.gone();
      return;
    }
    timer = setTimeout(connect, backoffMs(attempt++));
  }

  connect();
  return stop;
}
