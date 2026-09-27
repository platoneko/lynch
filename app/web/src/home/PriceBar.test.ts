import { describe, expect, it } from 'vitest';
import { barGeometry, barScale, labelShift, zoneRange } from './PriceBar';

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
    const scale = barScale([BASE, cand]);
    expect(scale).toEqual({ lo: 10, hi: 60 });
    expect(barGeometry(35, BASE, scale)).toMatchObject({ L: 0, H: 60, price: 50 });
    expect(barGeometry(35, cand, scale)).toMatchObject({ L: 20, H: 100, price: 50 });
  });
  it('widens the shared scale down to a candidate below the base (AVGO shape)', () => {
    const base = { L: 200, M: 300, H: 400 };
    const cand = { L: 150, M: 250, H: 350 };
    const scale = barScale([base, cand]);
    expect(scale).toEqual({ lo: 150, hi: 400 });
    expect(barGeometry(260, base, scale)).toMatchObject({ L: 20, H: 100, price: 44, overflow: null });
    expect(barGeometry(260, cand, scale)).toMatchObject({ L: 0, H: 80, price: 44, overflow: null });
  });
  it('spans every shown band: base, +V and candidate', () => {
    // +V credits H most (p_H = 1), a candidate can sit lower: each end comes from a different band.
    const option = { L: 10, M: 25, H: 70 };
    const cand = { L: 8, M: 18, H: 30 };
    const scale = barScale([BASE, option, cand]);
    expect(scale).toEqual({ lo: 8, hi: 70 });
    expect(barGeometry(39, option, scale)).toMatchObject({ H: 100, price: 50 });
    expect(barGeometry(39, cand, scale)).toMatchObject({ L: 0, price: 50 });
  });
  it('with only the base band the scale is L..H', () => {
    expect(barScale([BASE])).toEqual(OWN);
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

describe('labelShift', () => {
  it('centres interior labels under their tick', () => {
    for (const at of [10, 20, 50, 80, 90]) expect(labelShift(at)).toBe(-50);
  });
  it('keeps labels at the ends flush with the bar', () => {
    expect(labelShift(0)).toBeCloseTo(0);
    expect(labelShift(100)).toBe(-100);
  });
  it('eases between the end and the centred shift near each end', () => {
    expect(labelShift(5)).toBe(-25);
    expect(labelShift(95)).toBe(-75);
  });
});

describe('zoneRange', () => {
  it('bounds the in-frame zones by the anchors and cut points', () => {
    expect(zoneRange('conservative', BASE)).toBe('10.00–15.00');
    expect(zoneRange('neutral', BASE)).toBe('15.00–30.00');
    expect(zoneRange('optimistic', BASE)).toBe('30.00–40.00');
  });
  it('leaves the outer zones open-ended', () => {
    expect(zoneRange('deep_discount', BASE)).toBe('< 10.00');
    expect(zoneRange('stretched', BASE)).toBe('> 40.00');
  });
});
