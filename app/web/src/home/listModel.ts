import type { TickerRow, ZoneId } from '../types';

/**
 * The zone a row shows, filters and sorts by. BLOCKED never has one
 * (AGENTS.md「BLOCKED — 题头契约」); the server sends none, and this holds it
 * here too.
 */
export function rowZone(r: TickerRow): ZoneId | null {
  return r.blocked ? null : r.band?.zone ?? null;
}

export type SortKey = 'zone' | 'market' | 'ticker';

export interface HomeFilter {
  query: string;
  zones: string[];
  classes: string[];
  markets: string[];
  sort: SortKey;
}

export const DEFAULT_FILTER: HomeFilter = { query: '', zones: [], classes: [], markets: [], sort: 'zone' };

export function applyFilter(rows: TickerRow[], f: HomeFilter, zoneOrder: string[]): TickerRow[] {
  const serverOrder = new Map(rows.map((r, i) => [r.ticker, i]));
  const kept = rows.filter(
    (r) => matchesQuery(r, f.query) && matchesZones(r, f.zones) && matchesClasses(r, f.classes)
      && matchesMarkets(r, f.markets),
  );
  return kept.sort(comparator(f.sort, zoneOrder, serverOrder));
}

function matchesQuery(r: TickerRow, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return r.ticker.toLowerCase().includes(needle) || (r.name ?? '').toLowerCase().includes(needle);
}

function matchesZones(r: TickerRow, zones: string[]): boolean {
  if (zones.length === 0) return true;
  const zone = rowZone(r);
  return zone != null && zones.includes(zone);
}

function matchesClasses(r: TickerRow, classes: string[]): boolean {
  if (classes.length === 0) return true;
  return [r.lynch_primary, r.lynch_secondary].some((c) => c != null && classes.includes(c));
}

function matchesMarkets(r: TickerRow, markets: string[]): boolean {
  if (markets.length === 0) return true;
  return r.market != null && markets.includes(r.market);
}

function comparator(sort: SortKey, zoneOrder: string[], serverOrder: Map<string, number>) {
  const byTicker = (a: TickerRow, b: TickerRow) => a.ticker.localeCompare(b.ticker);
  if (sort === 'ticker') return byTicker;
  // The server already lists US → HK → A, name-sorted within (eval/lib/frameworks.py).
  if (sort === 'market') return (a: TickerRow, b: TickerRow) => serverOrder.get(a.ticker)! - serverOrder.get(b.ticker)!;
  // Rows without a zone (BLOCKED, no band, parse error) sink to the bottom.
  const rank = (r: TickerRow) => {
    const zone = rowZone(r);
    const i = zone ? zoneOrder.indexOf(zone) : -1;
    return i < 0 ? zoneOrder.length : i;
  };
  return (a: TickerRow, b: TickerRow) => rank(a) - rank(b) || byTicker(a, b);
}

export function latestAsOf(rows: TickerRow[]): string | null {
  let best: string | null = null;
  for (const r of rows) {
    const t = r.band?.as_of;
    if (t && (best === null || Date.parse(t) > Date.parse(best))) best = t;
  }
  return best;
}

/** The card's Lynch class line in Chinese; an id the server has no label for shows as is. */
export function lynchClasses(r: TickerRow, label: (id: string) => string): string {
  return [r.lynch_primary, r.lynch_secondary].filter((c) => c != null).map(label).join(' / ');
}
