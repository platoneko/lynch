import { describe, expect, it } from 'vitest';
import { backoffMs } from './api';

describe('backoffMs', () => {
  it('doubles from one second and caps at thirty', () => {
    expect([0, 1, 2, 3, 4, 5, 6, 20].map(backoffMs)).toEqual([1000, 2000, 4000, 8000, 16_000, 30_000, 30_000, 30_000]);
  });
});
