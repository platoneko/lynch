export type AnomalySession = 'regular' | 'pre' | 'post' | 'overnight';

/** The largest move seen against one base close (server: anomalies.py). Times are Beijing ISO strings. */
export interface AnomalyHit {
  ticker: string;
  ref: string; // the base close, the event's key with the ticker
  first_at: string;
  move: number; // price / base close - 1
  mult: number; // |move| / the ticker's usual daily move
  session: AnomalySession;
  at: string;
  reason: 'move' | 'volume';
  volume_mult: number | null;
}

export interface AnomalyItem extends AnomalyHit {
  researched: boolean; // a 讨论 chat on the ticker was started after it first fired
  ignored: boolean;
}

export interface AnomalyList {
  error: string | null; // the latest quote fetch or baseline refresh failure
  items: AnomalyItem[]; // newest event per ticker, newest first
}
