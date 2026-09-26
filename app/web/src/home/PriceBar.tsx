import type { ZoneId } from '../types';

export interface Anchors {
  L: number;
  M: number;
  H: number;
}

/** The price range one card's bars share, so a candidate's shift against the base shows. */
export interface Scale {
  lo: number;
  hi: number;
}

// Closer than this (percent of the bar width) and the M number would collide with L's or H's.
const MIN_LABEL_GAP = 16;
// Within this distance (percent of the bar width) of an end, a centred number would spill past it.
const LABEL_EDGE = 10;

export function barScale(base: Anchors, candidate: Anchors | null): Scale {
  return candidate
    ? { lo: Math.min(base.L, candidate.L), hi: Math.max(base.H, candidate.H) }
    : { lo: base.L, hi: base.H };
}

/** Percent positions on `scale` of the anchors, the zone cut points A/B and the price. */
export function barGeometry(price: number, a: Anchors, scale: Scale) {
  const span = scale.hi - scale.lo;
  const pct = (v: number) => (span > 0 ? ((v - scale.lo) / span) * 100 : 50);
  // The zone cut points (AGENTS.md「价带约定」): conservative [L, A), neutral [A, B), optimistic [B, H].
  const A = (a.L + a.M) / 2;
  const B = (a.M + a.H) / 2;
  const [L, M, H] = [pct(a.L), pct(a.M), pct(a.H)];
  return {
    L, A: pct(A), M, B: pct(B), H,
    price: Math.min(Math.max(pct(price), 0), 100),
    overflow: price < scale.lo ? ('below' as const) : price > scale.hi ? ('above' as const) : null,
    showMidLabel: M - L >= MIN_LABEL_GAP && H - M >= MIN_LABEL_GAP,
  };
}

/** One band on a shared scale; the dot is the price, coloured by the zone it falls in for this band. */
export function PriceBar({ price, anchors, scale, zone }: {
  price: number;
  anchors: Anchors;
  scale: Scale;
  zone: ZoneId | null;
}) {
  const g = barGeometry(price, anchors, scale);
  const segments = [
    ['conservative', g.L, g.A],
    ['neutral', g.A, g.B],
    ['optimistic', g.B, g.H],
  ] as const;
  const labels = [
    [anchors.L, g.L],
    ...(g.showMidLabel ? [[anchors.M, g.M] as const] : []),
    [anchors.H, g.H],
  ];
  return (
    <div className="pricebar">
      <div className="pricebar-track">
        {segments.map(([z, from, to]) => (
          <div key={z} className={`pricebar-seg seg-${z}`} style={{ left: `${from}%`, width: `${to - from}%` }} />
        ))}
        <div className="pricebar-mid" style={{ left: `${g.M}%` }} />
        <div className={`pricebar-dot ${zone ? `zone-bg-${zone}` : 'dot-muted'}`} style={{ left: `${g.price}%` }}>
          {g.overflow === 'below' ? '◀' : g.overflow === 'above' ? '▶' : ''}
        </div>
      </div>
      <div className="pricebar-nums">
        {labels.map(([value, at], i) => (
          <span key={i} style={{ left: `${at}%`, transform: `translateX(${labelShift(at)}%)` }}>{fmt(value)}</span>
        ))}
      </div>
    </div>
  );
}

/**
 * Horizontal shift of a number label, as a percent of its own width: centred under its
 * tick (-50) in the interior, easing to flush-left (0) at the left end and flush-right
 * (-100) at the right end so a label never leaves the bar.
 */
export function labelShift(at: number): number {
  if (at < LABEL_EDGE) return -50 * (Math.max(at, 0) / LABEL_EDGE);
  if (at > 100 - LABEL_EDGE) return -50 - 50 * ((Math.min(at, 100) - (100 - LABEL_EDGE)) / LABEL_EDGE);
  return -50;
}

export function fmt(n: number): string {
  return n >= 1000 ? n.toFixed(0) : n >= 100 ? n.toFixed(1) : n.toFixed(2);
}
