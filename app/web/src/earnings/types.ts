import type { AnomalyHit } from '../anomalies/types';

export type EarningsSlot = 'before' | 'during' | 'after' | 'unknown';

/** One pool ticker's earnings row, judged by the server from the calendar and the market clock. */
export interface EarningsItem {
  ticker: string;
  status: 'released' | 'upcoming';
  date: string; // market-local release date, YYYY-MM-DD
  day_offset: number; // release date minus the market's local today
  slot: EarningsSlot;
  release_at: string; // ISO moment the release counts as out
  period: string; // the calendar's wording, e.g. "2026 财年第 3 季度"
  researched: boolean; // a 讨论 chat on the ticker was started after the release
  ignored: boolean;
  anomaly: AnomalyHit | null; // the ticker's anomaly filed under this row rather than the anomaly page
}

export interface EarningsList {
  fetched_day: string | null; // Beijing day of the last full calendar fetch
  error: string | null; // why the latest fetch failed; items then come from fetched_day
  items: EarningsItem[]; // released newest first, then upcoming soonest first
}
