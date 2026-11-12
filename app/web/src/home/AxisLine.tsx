import type { AxisInfo, TickerRow } from '../types';
import { fmt, priceDecimals } from './PriceBar';

/** Map the saved scenario to saved prices; this performs no valuation calculation. */
function scenarioPrices(row: TickerRow, tier: string): string {
  if (!row.currency) return '';
  const anchors: Record<string, number | null> = { L: row.L, M: row.M, H: row.H };
  const boundary = tier === '<L' ? 'L' : tier === '>H' ? 'H' : null;
  const keys = boundary ? [boundary] : tier.replace('-', '–').split('–');
  const prefix = tier === '<L' ? '低于 ' : tier === '>H' ? '高于 ' : '';
  if (keys.length > 2 || keys.some((key) => typeof anchors[key] !== 'number' || !Number.isFinite(anchors[key]))) return '';
  return `${prefix}${keys.map((key) => fmt(anchors[key]!, priceDecimals(row.ticker))).join('–')} ${row.currency}`;
}

/** Keep source and migration commentary on the document page, not the home scenario line. */
export function AxisLine({ row, onOpenAxis, showDetails = false }: {
  row: TickerRow;
  axes: ReadonlyMap<string, AxisInfo>;
  onOpenAxis: (name: string) => void;
  showDetails?: boolean;
}) {
  const details = row.axis_details ?? [];
  const priced = details.filter((axis) => axis.role === 'priced');
  const own = priced.length === 0 ? row.consensus : null;
  const links = details.map((axis) => {
    const c = axis.consensus;
    const isPriced = axis.role === 'priced';
    let text = axis.label;
    if (isPriced && c) {
      if (showDetails) {
        const source = c.kind === 'single_institution' ? `单家参考 ${c.institution}` : '历史共识';
        text += ` · ${source} ${c.tier}（${c.as_of}）${c.source_note ? ` · ${c.source_note}` : ''}`;
      } else {
        text += ` · ${c.kind === 'single_institution' ? '单家参考 ' : ''}${c.tier}`;
        const values = priced.length === 1 ? scenarioPrices(row, c.tier) : '';
        if (values) text += ` · ${values}`;
      }
    } else if (!isPriced && priced.length > 0) {
      text = `参照 ${text}`;
    }
    if (showDetails && axis.historical_supplement) text += axis.relationship_to_result === 'unverified'
      ? ' · 历史轴资料，旧估值关联待核' : ' · 历史轴资料';
    return { name: axis.name, text, ref: axis.role === 'reference' };
  });
  if (links.length === 0 && !own) return showDetails && row.axis_status === 'unknown'
    ? <div className="axis-line">轴资料待核对</div> : null;
  const ownPrices = own && !showDetails ? scenarioPrices(row, own.tier) : '';
  const ownSource = own?.kind === 'single_institution'
    ? `单家参考${showDetails && own.institution ? ` ${own.institution}` : ''} ` : '';
  return (
    <>
    {own && <div className="axis-line">{showDetails ? '本票情景梯' : '本票主推演'} · {ownSource}{own.tier}
      {ownPrices && ` · ${ownPrices}`}
      {showDetails && own.as_of && `（${own.as_of}）`}
      {showDetails && own.note && ` · ${own.note}`}
      {showDetails && own.relationship_to_result === 'unverified' && ' · 旧估值关联待核'}</div>}
    {links.length > 0 && <div className="axis-line">
      <span className="axis-caption">{showDetails ? '轴' : priced.some((axis) => axis.consensus) ? '主推演' : priced.length > 0 ? '情景轴' : '参照'}</span>
      {links.map((l) => (
        <button key={l.name} className={l.ref ? 'axis-link axis-ref' : 'axis-link'}
          // The card itself opens the ticker; the axis must not also trigger that.
          onClick={(e) => { e.stopPropagation(); onOpenAxis(l.name); }}>
          {l.text}
        </button>
      ))}
    </div>}
    {showDetails && row.axis_status === 'unknown' && <div className="axis-line">轴资料待核对</div>}
    </>
  );
}
