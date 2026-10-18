import { describe, expect, it } from 'vitest';
import { offlineLabel } from './label';

describe('offlineLabel', () => {
  it('says when the home list was saved, in phone time', () => {
    expect(offlineLabel(new Date(2026, 9, 18, 20, 5).toISOString())).toBe('离线 · 数据存于 10-18 20:05');
  });

  it('is bare while the saved time is unknown', () => {
    expect(offlineLabel(null)).toBe('离线');
  });
});
