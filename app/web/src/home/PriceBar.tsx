export function barGeometry(price: number, L: number, M: number, H: number) {
  const span = H - L;
  const raw = (price - L) / span;
  return {
    pricePct: Math.min(Math.max(raw, 0), 1) * 100,
    midPct: ((M - L) / span) * 100,
    overflow: raw < 0 ? ('below' as const) : raw > 1 ? ('above' as const) : null,
  };
}

export function PriceBar({ price, L, M, H, zone }: { price: number; L: number; M: number; H: number; zone: string }) {
  const g = barGeometry(price, L, M, H);
  return (
    <div className="pricebar">
      <span className="pricebar-end">{fmt(L)}</span>
      <div className="pricebar-track">
        <div className="pricebar-mid" style={{ left: `${g.midPct}%` }} />
        <div className={`pricebar-dot zone-bg-${zone}`} style={{ left: `${g.pricePct}%` }}>
          {g.overflow === 'below' ? '◀' : g.overflow === 'above' ? '▶' : ''}
        </div>
      </div>
      <span className="pricebar-end">{fmt(H)}</span>
    </div>
  );
}

export function fmt(n: number): string {
  return n >= 1000 ? n.toFixed(0) : n >= 100 ? n.toFixed(1) : n.toFixed(2);
}
