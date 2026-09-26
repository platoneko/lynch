import type { TickerRow } from '../types';
import { rowZone } from './listModel';
import { fmt, PriceBar } from './PriceBar';

export function TickerCard({ row, zoneLabel, onOpen }: {
  row: TickerRow;
  zoneLabel: (id: string) => string;
  onOpen: (ticker: string) => void;
}) {
  return (
    <button className="card" onClick={() => onOpen(row.ticker)}>
      <div className="card-line1">
        <span className="card-ticker">{row.ticker}</span>
        <span className="card-name">{row.name ?? ''}</span>
        {row.band && <span className="card-price">{fmt(row.band.price)} {row.currency}</span>}
      </div>
      {row.error ? (
        <div className="card-error">解析失败：{row.error}</div>
      ) : (
        <>
          <div className="card-classes">
            {row.lynch_primary}
            {row.lynch_secondary && ` / ${row.lynch_secondary}`}
          </div>
          <CardZone row={row} zoneLabel={zoneLabel} />
        </>
      )}
    </button>
  );
}

function CardZone({ row, zoneLabel }: { row: TickerRow; zoneLabel: (id: string) => string }) {
  // BLOCKED never shows a zone (AGENTS.md「BLOCKED — 题头契约」).
  if (row.blocked) return <div className="tag tag-blocked">BLOCKED</div>;
  const zone = rowZone(row);
  if (!row.band || !zone) return <div className="tag tag-muted">暂无日报</div>;
  const { band } = row;
  return (
    <>
      <div className="card-zone">
        <span className={`tag zone-bg-${zone}`}>{zoneLabel(zone)}</span>
        <PriceBar price={band.price} L={row.L!} M={row.M!} H={row.H!} zone={zone} />
      </div>
      {(band.option_zone || band.candidate_zone) && (
        <div className="card-extra">
          {band.option_zone && <span>+V {zoneLabel(band.option_zone)}</span>}
          {band.candidate_zone && <span>候选 {zoneLabel(band.candidate_zone)}</span>}
        </div>
      )}
    </>
  );
}
