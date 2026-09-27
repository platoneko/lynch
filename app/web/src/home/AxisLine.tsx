import type { AxisInfo, TickerRow } from '../types';
import { consensusText } from './listModel';

/**
 * 「轴 电解铝价差 · 共识 L」 per mounted axis, each opening that axis's document.
 * A ticker without a scenario axis shows its own ladder's consensus line, if it wrote one.
 */
export function AxisLine({ row, axes, onOpenAxis }: {
  row: TickerRow;
  axes: ReadonlyMap<string, AxisInfo>;
  onOpenAxis: (name: string) => void;
}) {
  const mounted = (row.axes ?? []).flatMap((name) => axes.get(name) ?? []);
  if (mounted.length === 0) {
    if (!row.consensus) return null;
    return <div className="axis-line"><span className="axis-own">本票情景梯 · {consensusText(row.consensus)}</span></div>;
  }
  return (
    <div className="axis-line">
      <span className="axis-caption">轴</span>
      {mounted.map((a) => (
        <button key={a.name} className="axis-link"
          // The card itself opens the ticker; the axis must not also trigger that.
          onClick={(e) => { e.stopPropagation(); onOpenAxis(a.name); }}>
          {a.label} · {consensusText(a.consensus)}
        </button>
      ))}
    </div>
  );
}
