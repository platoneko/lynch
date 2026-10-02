import type { AxisInfo, TickerRow } from '../types';
import { consensusText } from './listModel';

/**
 * 「轴 电解铝价差 · 共识 L」 per rating axis, then each reference axis by name alone — the
 * axis consensus is not that ticker's (contracts.md「定档 / 参照挂载」). Each opens its axis document.
 * A ticker without a scenario axis shows its own ladder's consensus line, if it wrote one.
 */
export function AxisLine({ row, axes, onOpenAxis }: {
  row: TickerRow;
  axes: ReadonlyMap<string, AxisInfo>;
  onOpenAxis: (name: string) => void;
}) {
  const lookup = (names: string[] | null) => (names ?? []).flatMap((name) => axes.get(name) ?? []);
  const links = [
    ...lookup(row.axes).map((a) => ({ name: a.name, text: `${a.label} · ${consensusText(a.consensus)}`, ref: false })),
    ...lookup(row.ref_axes).map((a) => ({ name: a.name, text: a.label, ref: true })),
  ];
  if (links.length === 0) {
    if (!row.consensus) return null;
    return <div className="axis-line"><span className="axis-own">本票情景梯 · {consensusText(row.consensus)}</span></div>;
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
    </div>
  );
}
