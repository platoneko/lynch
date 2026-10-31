import type { EarningsSlot } from './earnings/types';

export type ZoneId = 'deep_discount' | 'conservative' | 'neutral' | 'optimistic' | 'stretched';

export interface Labeled {
  id: string;
  label: string;
}

/** Zones of the latest Longbridge quote, computed by the server with the daily digest's rules. */
export interface Band {
  price: number;
  session: 'pre' | 'post' | 'overnight' | null; // a US extended-hours price; null for the regular session
  as_of: string; // when the server fetched the quote
  zone: ZoneId | null;
  option_zone: ZoneId | null;
  candidate_zone: ZoneId | null;
}

/** A framework's unmodelled option (AGENTS.md「未建模期权」): the band credited with p_k · V_opt. */
export interface OptionBand {
  v_opt: number;
  L: number;
  M: number;
  H: number;
  line: string | null; // the Outputs `\(V_{opt}\)` bullet, raw markdown
  rows: { id: string; text: string }[]; // Assumptions rows that line cites: id and text cell, raw markdown
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

/** Where sell-side consensus sits on a ladder (AGENTS.md「共享情景轴」共识行). */
export interface Consensus {
  tier: string; // L | M | H | L–M | M–H | <L | >H
  note: string; // source and as-of, shown as is
}

/** A shared scenario axis (the rates axis is left out by the server). */
export interface AxisInfo {
  name: string; // file stem, the route and filter id
  label: string; // short name for chips and cards
  title: string;
  consensus: Consensus | null;
  tickers: string[]; // rating mounts: the axis tiers set their L/M/H, so its consensus is theirs
  refs: string[]; // reference mounts: they cite or check the axis, its consensus is not theirs
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
  option: OptionBand | null;
  candidate: Candidate | null;
  axes: string[] | null; // names of the scenario axes that set its L/M/H (rating mounts)
  ref_axes: string[] | null; // names of the scenario axes it only cites or checks (reference mounts)
  consensus: Consensus | null; // its own header line, shown only when it has no axis
  band: Band | null;
  quote_error: string | null; // why `band` is null for this row alone
  error: string | null;
  archived: boolean; // put away from the list in the app; its document and eval script stay
  framework_updated: string | null; // the header's 更新 day, YYYY-MM-DD
  // From the server's earnings calendar; null for archived tickers, which it does not cover.
  last_earnings: EarningsDate | null; // the latest release out within 30 days
  next_earnings: EarningsDate | null;
  stale_after_earnings: boolean; // the framework was not revised after last_earnings
}

/** A release on a ticker's row, judged on the market's clock by the server (earnings.py). */
export interface EarningsDate {
  date: string; // market-local release date, YYYY-MM-DD
  slot: EarningsSlot;
  period: string;
  release_at: string;
  day_offset: number; // release date minus the market's local today
}

export interface Catalog {
  zones: Labeled[];
  lynch_classes: Labeled[];
  quote_error: string | null; // the whole quote call failed: every `band` is null
  axes: AxisInfo[];
  tickers: TickerRow[];
}
