import type { Catalog } from '../types';
import { applyFilter, latestAsOf, type HomeFilter, type SortKey } from './listModel';
import { TickerCard } from './TickerCard';

const SORTS: { id: SortKey; label: string }[] = [
  { id: 'zone', label: '按 zone' },
  { id: 'market', label: '按市场' },
  { id: 'ticker', label: '按代码' },
];

const MARKETS: { id: string; label: string }[] = [
  { id: 'A', label: 'A股' },
  { id: 'HK', label: '港股' },
  { id: 'US', label: '美股' },
];

export function HomePage({ catalog, filter, onFilter, onOpen, onRefresh, refreshing }: {
  catalog: Catalog;
  filter: HomeFilter;
  onFilter: (f: HomeFilter) => void;
  onOpen: (ticker: string) => void;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const zoneOrder = catalog.zones.map((z) => z.id);
  const labels = new Map([...catalog.zones, ...catalog.lynch_classes].map((x) => [x.id, x.label]));
  const rows = applyFilter(catalog.tickers, filter, zoneOrder);
  const asOf = latestAsOf(catalog.tickers);
  const toggle = (list: string[], id: string) => (list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);

  return (
    <div className="home">
      <header className="home-top">
        <div className="home-search-row">
          <input
            className="home-search"
            type="search"
            placeholder="代码 / 名字"
            value={filter.query}
            onChange={(e) => onFilter({ ...filter, query: e.target.value })}
          />
          <button className="icon-btn" onClick={onRefresh} disabled={refreshing} aria-label="刷新">
            {refreshing ? '…' : '⟳'}
          </button>
        </div>
        <div className="chips">
          {catalog.zones.map((z) => (
            <button key={z.id}
              className={`chip ${filter.zones.includes(z.id) ? `chip-on zone-bg-${z.id}` : ''}`}
              onClick={() => onFilter({ ...filter, zones: toggle(filter.zones, z.id) })}>
              {z.label}
            </button>
          ))}
        </div>
        <div className="chips">
          {catalog.lynch_classes.map((c) => (
            <button key={c.id}
              className={`chip ${filter.classes.includes(c.id) ? 'chip-on' : ''}`}
              onClick={() => onFilter({ ...filter, classes: toggle(filter.classes, c.id) })}>
              {c.label}
            </button>
          ))}
        </div>
        <div className="home-meta">
          <div className="chips">
            {MARKETS.map((m) => (
              <button key={m.id}
                className={`chip ${filter.markets.includes(m.id) ? 'chip-on' : ''}`}
                onClick={() => onFilter({ ...filter, markets: toggle(filter.markets, m.id) })}>
                {m.label}
              </button>
            ))}
          </div>
          <select value={filter.sort} onChange={(e) => onFilter({ ...filter, sort: e.target.value as SortKey })}>
            {SORTS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
          <span>{rows.length} / {catalog.tickers.length}</span>
        </div>
      </header>
      {catalog.quote_error && <p className="banner-muted">{catalog.quote_error}</p>}
      <main className="cards">
        {rows.map((r) => (
          <TickerCard key={r.ticker} row={r} zoneLabel={(id) => labels.get(id) ?? id} onOpen={onOpen} />
        ))}
        {rows.length === 0 && <p className="empty">没有匹配的标的</p>}
      </main>
      <footer className="home-foot">取价时间：{asOf ? asOf.replace('T', ' ').slice(0, 16) : '无'}</footer>
    </div>
  );
}
