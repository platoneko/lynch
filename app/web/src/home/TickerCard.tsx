import type { TickerRow } from '../types';
import { rowZone } from './listModel';
import { barScale, fmt, PriceBar } from './PriceBar';

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
  const { band, candidate } = row;
  const base = { L: row.L!, M: row.M!, H: row.H! };
  // Both bars share one scale so the candidate's shift against the base is visible.
  const scale = barScale(base, candidate);
  return (
    <>
      {/* Alone, the base band keeps its one-line card layout; with a candidate the two stack. */}
      <div className={candidate ? 'band' : 'band band-inline'}>
        <div className="band-head">
          {candidate && <span className="band-caption">底牌</span>}
          <span className={`tag zone-bg-${zone}`}>{zoneLabel(zone)}</span>
        </div>
        <PriceBar price={band.price} anchors={base} scale={scale} zone={zone} />
      </div>
      {candidate && (
        <div className="band">
          <div className="band-head">
            <span className="band-caption">候选</span>
            {/* Zones come only from the daily run; a missing one is shown as such, never computed here. */}
            {band.candidate_zone
              ? <span className={`tag zone-bg-${band.candidate_zone}`}>{zoneLabel(band.candidate_zone)}</span>
              : <span className="tag tag-muted">—</span>}
            <span className="band-label">{candidate.label}</span>
          </div>
          <PriceBar price={band.price} anchors={candidate} scale={scale} zone={band.candidate_zone} />
        </div>
      )}
      {band.option_zone && <div className="card-extra">+V {zoneLabel(band.option_zone)}</div>}
    </>
  );
}
