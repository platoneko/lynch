import { get, post } from '../api';
import type { AnomalyList } from './types';

export async function fetchAnomalies(): Promise<AnomalyList> {
  return (await get('/api/anomalies')).json();
}

export async function ignoreAnomaly(ticker: string, ref: string): Promise<void> {
  await post('/api/anomalies/ignore', { ticker, ref });
}

export async function unignoreAnomaly(ticker: string, ref: string): Promise<void> {
  await post('/api/anomalies/unignore', { ticker, ref });
}
