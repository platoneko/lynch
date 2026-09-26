export type ZoneId = 'deep_discount' | 'conservative' | 'neutral' | 'optimistic' | 'stretched';

export interface Labeled {
  id: string;
  label: string;
}

export interface Band {
  price: number;
  as_of: string;
  zone: ZoneId | null;
  option_zone: ZoneId | null;
  candidate_zone: ZoneId | null;
}

/** One pool member; when `error` is set only `ticker` is filled. */
export interface TickerRow {
  ticker: string;
  name: string | null;
  market: 'US' | 'HK' | 'A' | null;
  lynch_primary: string | null;
  lynch_secondary: string | null;
  currency: string | null;
  L: number | null;
  M: number | null;
  H: number | null;
  blocked: string | null;
  candidate_label: string | null;
  band: Band | null;
  error: string | null;
}

export interface Catalog {
  zones: Labeled[];
  lynch_classes: Labeled[];
  tickers: TickerRow[];
}
