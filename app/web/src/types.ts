export type ZoneId = 'deep_discount' | 'conservative' | 'neutral' | 'optimistic' | 'stretched';

export interface Labeled {
  id: string;
  label: string;
}

/** Zones of the latest Longbridge quote, computed by the server with the daily digest's rules. */
export interface Band {
  price: number;
  as_of: string; // when the server fetched the quote
  zone: ZoneId | null;
  option_zone: ZoneId | null;
  candidate_zone: ZoneId | null;
}

/** A framework's consensus-upgrade candidate band (AGENTS.md「共识升级候选带」). */
export interface Candidate {
  label: string;
  L: number;
  M: number;
  H: number;
  gate: string | null; // the Assumptions row id that must hold before it replaces the base band
  background: string | null; // that row's text cell, raw markdown
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
  candidate: Candidate | null;
  band: Band | null;
  quote_error: string | null; // why `band` is null for this row alone
  error: string | null;
}

export interface Catalog {
  zones: Labeled[];
  lynch_classes: Labeled[];
  quote_error: string | null; // the whole quote call failed: every `band` is null
  tickers: TickerRow[];
}
