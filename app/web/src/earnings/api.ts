import { get, post } from '../api';
import type { EarningsList } from './types';

export async function fetchEarnings(): Promise<EarningsList> {
  return (await get('/api/earnings')).json();
}

export async function ignoreEarnings(ticker: string, date: string): Promise<void> {
  await post('/api/earnings/ignore', { ticker, date });
}

export async function unignoreEarnings(ticker: string, date: string): Promise<void> {
  await post('/api/earnings/unignore', { ticker, date });
}
