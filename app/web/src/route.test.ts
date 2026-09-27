import { describe, expect, it } from 'vitest';
import { axisHash, docHash, parseRoute } from './route';

describe('route', () => {
  it('parses doc and falls back to home', () => {
    expect(parseRoute('#/doc/NVDA.US')).toEqual({ page: 'doc', ticker: 'NVDA.US' });
    expect(parseRoute('')).toEqual({ page: 'home' });
    expect(parseRoute('#/')).toEqual({ page: 'home' });
    expect(parseRoute('#/doc/a/b')).toEqual({ page: 'home' });
  });
  it('parses axis pages', () => {
    expect(parseRoute('#/axis/fed-path-aluminium')).toEqual({ page: 'axis', name: 'fed-path-aluminium' });
    expect(parseRoute('#/axis/a/b')).toEqual({ page: 'home' });
    expect(parseRoute(axisHash('coal-price'))).toEqual({ page: 'axis', name: 'coal-price' });
  });
  it('round-trips', () => {
    expect(parseRoute(docHash('0700.HK'))).toEqual({ page: 'doc', ticker: '0700.HK' });
  });
});
