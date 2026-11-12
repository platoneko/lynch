import type { AxisInfo, TickerRow } from '../types';

/**
 * 「轴 电解铝价差 · 共识 L」 per rating axis, then each reference axis by name alone — the
 * axis consensus is not that ticker's (contracts.md「定档 / 参照挂载」). Each opens its axis document.
 * A ticker without a scenario axis shows its own ladder's consensus line, if it wrote one.
 */
export function AxisLine({ row, onOpenAxis }: {
  row: TickerRow;
  axes: ReadonlyMap<string, AxisInfo>;
  onOpenAxis: (name: string) => void;
}) {
  const links = (row.axis_details ?? []).map((axis) => {
    const c = axis.consensus;
    const label = c?.kind === 'single_institution' ? `单家参考 ${c.institution}` : '历史共识';
    let text = axis.role === 'priced'
      ? `${axis.label} · ${c ? `${label} ${c.tier}（${c.as_of}）${c.source_note ? ` · ${c.source_note}` : ''}` : '共识未标注'}` : axis.label;
    if (axis.historical_supplement) text += axis.relationship_to_result === 'unverified'
      ? ' · 历史轴资料，旧估值关联待核' : ' · 历史轴资料';
    return { name: axis.name, text, ref: axis.role === 'reference' };
  });
  if (links.length === 0) {
    if (row.consensus) return <div className="axis-line">本票情景梯 · {row.consensus.kind === 'single_institution' ? `单家参考 ${row.consensus.institution}` : '历史共识'} {row.consensus.tier}（{row.consensus.as_of}）
      {row.consensus.note && ` · ${row.consensus.note}`}
      {row.consensus.relationship_to_result === 'unverified' && ' · 旧估值关联待核'}
      {row.axis_status === 'unknown' && ' · 轴资料待核对'}</div>;
    if (row.axis_status === 'unknown') return <div className="axis-line">轴资料待核对</div>;
    return null;
  }
  return (
    <div className="axis-line">
      <span className="axis-caption">轴</span>
      {links.map((l) => (
        <button key={l.name} className={l.ref ? 'axis-link axis-ref' : 'axis-link'}
          // The card itself opens the ticker; the axis must not also trigger that.
          onClick={(e) => { e.stopPropagation(); onOpenAxis(l.name); }}>
          {l.text}
        </button>
      ))}
      {row.axis_status === 'unknown' && <span>轴资料待核对</span>}
    </div>
  );
}
