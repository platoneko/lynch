import { useState } from 'react';
import { Menu, type MenuItem } from '../Menu';
import type { Connection } from '../offline/connection';
import { offlineLabel } from '../offline/label';
import { menuLabel as anomalyMenuLabel, pendingCount as anomalyPendingCount } from '../anomalies/model';
import type { AnomalyList } from '../anomalies/types';
import { menuLabel, pendingCount } from '../earnings/model';
import type { EarningsList } from '../earnings/types';
import type { Catalog } from '../types';
import { menuLabel as watchMenuLabel, pendingCount as watchPendingCount } from '../watch/model';
import type { WatchList } from '../watch/types';
import { applyFilter, inArchiveScope, latestAsOf, quoteTimeText, type HomeFilter, type SortKey } from './listModel';
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

export function HomePage({ catalog, filter, onFilter, onOpen, onOpenAxis,
  chatCount, onOpenChats, earnings, onOpenEarnings, anomalies, onOpenAnomalies, watch, onOpenWatch, connection }: {
  catalog: Catalog;
  filter: HomeFilter;
  onFilter: (f: HomeFilter) => void;
  onOpen: (ticker: string) => void;
  onOpenAxis: (name: string) => void;
  chatCount: number;
  onOpenChats: () => void;
  earnings: EarningsList | null;
  onOpenEarnings: () => void;
  anomalies: AnomalyList | null;
  onOpenAnomalies: () => void;
  watch: WatchList | null;
  onOpenWatch: () => void;
  connection: Connection;
}) {
  const { offline } = connection;
  const alertsPending = !offline
    && (pendingCount(earnings) > 0 || anomalyPendingCount(anomalies) > 0 || watchPendingCount(watch) > 0);
  const earningsLabel = menuLabel(earnings);
  const anomaliesLabel = anomalyMenuLabel(anomalies);
  const watchLabel = watchMenuLabel(watch);
  // Offline these pages have nothing to show (the server keeps them): always listed, greyed, without counts.
  const alertRows: MenuItem[] = offline ? [
    { label: '财报', onSelect: onOpenEarnings, disabled: true },
    { label: '异动', onSelect: onOpenAnomalies, disabled: true },
    { label: '观察', onSelect: onOpenWatch, disabled: true },
  ] : [
    ...(earningsLabel ? [{ label: earningsLabel, onSelect: onOpenEarnings }] : []),
    ...(anomaliesLabel ? [{ label: anomaliesLabel, onSelect: onOpenAnomalies }] : []),
    ...(watchLabel ? [{ label: watchLabel, onSelect: onOpenWatch }] : []),
  ];
  const chatRow: MenuItem = offline
    ? { label: '对话', onSelect: onOpenChats, disabled: true }
    : { label: chatCount > 0 ? `对话 · ${chatCount} 个运行中` : '对话', onSelect: onOpenChats };
  const zoneOrder = catalog.zones.map((z) => z.id);
  const labels = new Map([...catalog.zones, ...catalog.lynch_classes].map((x) => [x.id, x.label]));
  const rows = applyFilter(catalog.tickers, filter, zoneOrder);
  const total = catalog.tickers.filter((r) => inArchiveScope(r, filter)).length;
  const archivedCount = catalog.tickers.filter((r) => r.archived).length;
  const staleCount = catalog.tickers.filter((r) => inArchiveScope(r, filter) && r.stale_after_earnings).length;
  const asOf = latestAsOf(catalog.tickers);
  const toggle = (list: string[], id: string) => (list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);
  const axes = new Map(catalog.axes.map((a) => [a.name, a]));
  // A dozen axis chips would crowd the header; they stay folded behind one toggle.
  const [axesOpen, setAxesOpen] = useState(false);
  const [menu, setMenu] = useState(false);

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
          <span className="quote-time">{quoteTimeText(asOf)}</span>
          {/* Badges stay on the button so running chats and pending alerts show without opening the menu. */}
          <button className="icon-btn has-badge" onClick={() => setMenu(!menu)} aria-label="更多">
            ⋯{!offline && chatCount > 0 && <span className="badge">{chatCount}</span>}
            {alertsPending && <span className="badge badge-alert">!</span>}
          </button>
          {menu && (
            <Menu onClose={() => setMenu(false)} items={[
              ...alertRows,
              chatRow,
              {
                label: filter.showArchived ? '隐藏归档' : `展示归档${archivedCount > 0 ? `（${archivedCount}）` : ''}`,
                onSelect: () => onFilter({ ...filter, showArchived: !filter.showArchived }),
              },
            ]} />
          )}
        </div>
        {offline && <div className="offline-line">{offlineLabel(connection.savedAt)}</div>}
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
        <div className={`chips ${axesOpen ? 'chips-wrap' : ''}`}>
          <button className={`chip ${filter.axes.length > 0 ? 'chip-on' : ''}`} onClick={() => setAxesOpen(!axesOpen)}>
            轴{filter.axes.length > 0 ? ` ${filter.axes.length}` : ''} {axesOpen ? '▴' : '▾'}
          </button>
          {axesOpen && catalog.axes.map((a) => (
            <button key={a.name}
              className={`chip ${filter.axes.includes(a.name) ? 'chip-on' : ''}`}
              onClick={() => onFilter({ ...filter, axes: toggle(filter.axes, a.name) })}>
              {a.label}
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
            <button className={`chip ${filter.staleOnly ? 'chip-on' : ''}`}
              onClick={() => onFilter({ ...filter, staleOnly: !filter.staleOnly })}>
              财报后未更新{staleCount > 0 ? `（${staleCount}）` : ''}
            </button>
          </div>
          <select value={filter.sort} onChange={(e) => onFilter({ ...filter, sort: e.target.value as SortKey })}>
            {SORTS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
          <span>{rows.length} / {total}</span>
        </div>
      </header>
      {catalog.quote_error && <p className="banner-muted">{catalog.quote_error}</p>}
      <main className="cards">
        {rows.map((r) => (
          <TickerCard key={r.ticker} row={r} label={(id) => labels.get(id) ?? id} axes={axes}
            onOpen={onOpen} onOpenAxis={onOpenAxis} />
        ))}
        {rows.length === 0 && <p className="empty">没有匹配的标的</p>}
      </main>
    </div>
  );
}
