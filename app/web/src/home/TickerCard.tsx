import type { TickerRow, ZoneId } from '../types';
import { lynchClasses, rowZone } from './listModel';
import { barScale, fmt, PriceBar } from './PriceBar';

export function TickerCard({ row, label, onOpen }: {
  row: TickerRow;
  label: (id: string) => string; // zone and Lynch class ids → Chinese labels
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
          <div className="card-classes">{lynchClasses(row, label)}</div>
          <CardZone row={row} label={label} />
        </>
      )}
    </button>
  );
}

function CardZone({ row, label }: { row: TickerRow; label: (id: string) => string }) {
  // BLOCKED never shows a zone (AGENTS.md「BLOCKED — 题头契约」).
  if (row.blocked) return <div className="tag tag-blocked">BLOCKED</div>;
  const zone = rowZone(row);
  if (!row.band || !zone) return <div className="tag tag-muted">{row.quote_error ?? '无报价'}</div>;
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
          <span className={`tag zone-bg-${zone}`}>{label(zone)}</span>
        </div>
        <PriceBar price={band.price} anchors={base} scale={scale} zone={zone} />
      </div>
      {candidate && (
        <div className="band">
          <CandidateHead label={candidate.label} zone={band.candidate_zone} zoneLabel={label} />
          <PriceBar price={band.price} anchors={candidate} scale={scale} zone={band.candidate_zone} />
        </div>
      )}
      {band.option_zone && <div className="card-extra">+V {label(band.option_zone)}</div>}
    </>
  );
}

/** 「候选 [zone] label」, shared by the card and the doc page header. */
export function CandidateHead({ label, zone, zoneLabel }: {
  label: string;
  zone: ZoneId | null;
  zoneLabel: (id: string) => string;
}) {
  return (
    <div className="band-head">
      <span className="band-caption">候选</span>
      {/* Zones come only from the server; a missing one is shown as such, never computed here. */}
      {zone
        ? <span className={`tag zone-bg-${zone}`}>{zoneLabel(zone)}</span>
        : <span className="tag tag-muted">—</span>}
      <span className="band-label">{label}</span>
    </div>
  );
}
