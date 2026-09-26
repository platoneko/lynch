import { describe, expect, it } from 'vitest';
import { barGeometry } from './PriceBar';

describe('barGeometry', () => {
  it('places price and M between L and H', () => {
    expect(barGeometry(25, 10, 20, 40)).toEqual({ pricePct: 50, midPct: (10 / 30) * 100, overflow: null });
  });
  it('pins to the ends with an overflow marker', () => {
    expect(barGeometry(5, 10, 20, 40)).toMatchObject({ pricePct: 0, overflow: 'below' });
    expect(barGeometry(99, 10, 20, 40)).toMatchObject({ pricePct: 100, overflow: 'above' });
  });
  it('treats L and H themselves as inside', () => {
    expect(barGeometry(10, 10, 20, 40).overflow).toBeNull();
    expect(barGeometry(40, 10, 20, 40).overflow).toBeNull();
  });
});
