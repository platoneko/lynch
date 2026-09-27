import type { TickerRow, ZoneId } from '../types';
import { EXTRA_CAPTION, extraBands, lynchClasses, rowZone, type ExtraBand } from './listModel';
import { barScale, fmt, PriceBar, zoneRange, type Anchors } from './PriceBar';

const SESSION_CN = { pre: '盘前', post: '盘后', overnight: '夜盘' } as const;

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
        {row.band && (
          <span className="card-price">
            {row.band.session && <span className="card-session">{SESSION_CN[row.band.session]}</span>}
            {fmt(row.band.price)} {row.currency}
          </span>
        )}
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
  const zone = rowZone(row);
  if (!row.band || !zone) return <div className="tag tag-muted">{row.quote_error ?? '无报价'}</div>;
  const { band } = row;
  const base = { L: row.L!, M: row.M!, H: row.H! };
  const extras = extraBands(row);
  // All bars share one scale so each extra band's shift against the base is visible.
  const scale = barScale([base, ...extras.map((e) => e.anchors)]);
  return (
    <>
      <div className="band">
        <div className="band-head">
          {extras.length > 0 && <span className="band-caption">底牌</span>}
          <ZoneTag zone={zone} anchors={base} label={label} />
        </div>
        <PriceBar price={band.price} anchors={base} scale={scale} zone={zone} />
      </div>
      {extras.map((e) => (
        <div key={e.kind} className="band">
          <BandHead band={e} zoneLabel={label} showRange />
          <PriceBar price={band.price} anchors={e.anchors} scale={scale} zone={e.zone} />
        </div>
      ))}
    </>
  );
}

/** The zone tag followed, on the card, by the price range of that zone on this band. */
function ZoneTag({ zone, anchors, label, showRange = true }: {
  zone: ZoneId;
  anchors: Anchors;
  label: (id: string) => string;
  showRange?: boolean;
}) {
  return (
    <>
      <span className={`tag zone-bg-${zone}`}>{label(zone)}</span>
      {showRange && <span className="zone-range">{zoneRange(zone, anchors)}</span>}
    </>
  );
}

/** 「+V [zone] +V 18.17」 or 「候选 [zone] label」, shared by the card and the doc page header. */
export function BandHead({ band, zoneLabel, showRange = false }: {
  band: ExtraBand;
  zoneLabel: (id: string) => string;
  showRange?: boolean; // the card shows the zone's price range; the doc page lists the prices elsewhere
}) {
  const { zone, label } = band;
  return (
    <div className="band-head">
      <span className="band-caption">{EXTRA_CAPTION[band.kind]}</span>
      {/* Zones come only from the server; a missing one is shown as such, never computed here. */}
      {zone
        ? <ZoneTag zone={zone} anchors={band.anchors} label={zoneLabel} showRange={showRange} />
        : <span className="tag tag-muted">—</span>}
      <span className="band-label">{label}</span>
    </div>
  );
}
