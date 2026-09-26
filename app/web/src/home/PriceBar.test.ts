import { describe, expect, it } from 'vitest';
import { barGeometry, barScale } from './PriceBar';

const BASE = { L: 10, M: 20, H: 40 };
const OWN = { lo: 10, hi: 40 };

describe('barGeometry', () => {
  it('places anchors, cut points and price on the scale', () => {
    expect(barGeometry(25, BASE, OWN)).toEqual({
      L: 0, A: (5 / 30) * 100, M: (10 / 30) * 100, B: (20 / 30) * 100, H: 100,
      price: 50, overflow: null, showMidLabel: true,
    });
  });
  it('puts the cut points at the anchor midpoints', () => {
    const g = barGeometry(25, { L: 100, M: 200, H: 500 }, { lo: 0, hi: 1000 });
    expect(g.A).toBeCloseTo(15);
    expect(g.B).toBeCloseTo(35);
  });
  it('shares one scale between base and candidate', () => {
    const cand = { L: 20, M: 30, H: 60 };
    const scale = barScale(BASE, cand);
    expect(scale).toEqual({ lo: 10, hi: 60 });
    expect(barGeometry(35, BASE, scale)).toMatchObject({ L: 0, H: 60, price: 50 });
    expect(barGeometry(35, cand, scale)).toMatchObject({ L: 20, H: 100, price: 50 });
  });
  it('without a candidate the scale is L..H', () => {
    expect(barScale(BASE, null)).toEqual(OWN);
  });
  it('pins to the ends with an overflow marker', () => {
    expect(barGeometry(5, BASE, OWN)).toMatchObject({ price: 0, overflow: 'below' });
    expect(barGeometry(99, BASE, OWN)).toMatchObject({ price: 100, overflow: 'above' });
  });
  it('treats the scale ends themselves as inside', () => {
    expect(barGeometry(10, BASE, OWN).overflow).toBeNull();
    expect(barGeometry(40, BASE, OWN).overflow).toBeNull();
  });
  it('drops the M number when it would crowd L or H', () => {
    expect(barGeometry(25, { L: 10, M: 12, H: 40 }, OWN).showMidLabel).toBe(false);
    expect(barGeometry(25, { L: 10, M: 38, H: 40 }, OWN).showMidLabel).toBe(false);
    // A narrow base on a wide shared scale crowds too.
    expect(barGeometry(25, BASE, { lo: 10, hi: 200 }).showMidLabel).toBe(false);
  });
  it('stays finite on a degenerate scale', () => {
    const g = barGeometry(5, { L: 10, M: 10, H: 10 }, { lo: 10, hi: 10 });
    for (const v of [g.L, g.A, g.M, g.B, g.H, g.price]) expect(Number.isFinite(v)).toBe(true);
    expect(g.overflow).toBe('below');
  });
});
