import { get, post } from '../api';
import type { WatchList } from './types';

export async function fetchWatch(): Promise<WatchList> {
  return (await get('/api/watch')).json();
}

/** Saves the card one reply carries; the server reads it back from the transcript. */
export async function saveWatch(convId: string, key: string): Promise<void> {
  await post(`/api/chats/${encodeURIComponent(convId)}/watch`, { key });
}

export async function removeWatch(ticker: string): Promise<void> {
  await post(`/api/watch/${encodeURIComponent(ticker)}/remove`);
}
