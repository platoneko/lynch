import { get, post } from '../api';
import { toChatEvent } from './chatModel';
import type { ChatEvent, ChatList, ChatOptions, Snapshot } from './types';

const base = (id: string) => `/api/chats/${encodeURIComponent(id)}`;

export async function fetchChats(): Promise<ChatList> {
  return (await get('/api/chats')).json();
}

export async function fetchChatOptions(): Promise<ChatOptions> {
  return (await get('/api/chats/options')).json();
}

export async function createChat(body: {
  kind: 'research' | 'ticker'; ticker: string | null; text: string; model: string; effort: string;
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

export async function archiveTicker(ticker: string): Promise<{ archived: number }> {
  return (await post('/api/chats/archive', { ticker })).json();
}

const EVENTS = ['delta', 'item', 'tool_result', 'state', 'usage', 'context', 'conv'];

/**
 * Follows one conversation. Every (re)connect starts with a snapshot that replaces the view, so the
 * browser's own EventSource retry is all the reconnect logic there is. Returns a closer.
 */
export function openChatEvents(id: string, on: {
  snapshot: (s: Snapshot) => void;
  event: (e: ChatEvent) => void;
  reset: () => void; // the branch changed (an edit): reopen for a fresh snapshot
  gone: () => void; // archived
}): () => void {
  const es = new EventSource(`${base(id)}/events`);
  es.addEventListener('snapshot', (m) => on.snapshot(JSON.parse((m as MessageEvent).data)));
  for (const type of EVENTS) {
    es.addEventListener(type, (m) => {
      const e = toChatEvent(type, JSON.parse((m as MessageEvent).data));
      if (e) on.event(e);
    });
  }
  es.addEventListener('reset', () => on.reset());
  es.addEventListener('archived', () => {
    es.close();
    on.gone();
  });
  return () => es.close();
}
