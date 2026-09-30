import { describe, expect, it } from 'vitest';
import { findHits, listMatches, stepMatch } from './search';

describe('findHits', () => {
  it('finds every occurrence, ignoring case', () => {
    expect(findHits('PE 与 pe 与 Pe', 'pe')).toEqual([
      { start: 0, end: 2 },
      { start: 5, end: 7 },
      { start: 10, end: 12 },
    ]);
  });

  it('takes the needle literally', () => {
    expect(findHits('r−g (x) r.g', 'r.g')).toEqual([{ start: 8, end: 11 }]);
    expect(findHits('(x)', '(x)')).toEqual([{ start: 0, end: 3 }]);
  });

  it('does not overlap hits', () => {
    expect(findHits('aaaa', 'aa')).toEqual([{ start: 0, end: 2 }, { start: 2, end: 4 }]);
  });

  it('finds nothing for an empty needle', () => {
    expect(findHits('abc', '')).toEqual([]);
  });
});

describe('listMatches', () => {
  it('numbers matches within each section, in document order', () => {
    expect(listMatches([2, 0, 1])).toEqual([
      { section: 0, nth: 0 },
      { section: 0, nth: 1 },
      { section: 2, nth: 0 },
    ]);
  });
});

describe('stepMatch', () => {
  const matches = listMatches([1, 0, 2, 1]); // sections 0, 2, 2, 3

  it('starts at the first match in or after the section being read', () => {
    expect(stepMatch(matches, null, -1, 1)).toBe(0);
    expect(stepMatch(matches, null, 1, 1)).toBe(1);
    expect(stepMatch(matches, null, 2, 1)).toBe(1);
  });

  it('starts backwards at the last match before the section being read', () => {
    expect(stepMatch(matches, null, 2, -1)).toBe(0);
    expect(stepMatch(matches, null, 0, -1)).toBe(3);
  });

  it('wraps past either end', () => {
    expect(stepMatch(matches, null, 5, 1)).toBe(0);
    expect(stepMatch(matches, 3, 0, 1)).toBe(0);
    expect(stepMatch(matches, 0, 0, -1)).toBe(3);
    expect(stepMatch(matches, 1, 0, 1)).toBe(2);
  });

  it('has nowhere to go without matches', () => {
    expect(stepMatch([], null, 0, 1)).toBeNull();
  });
});
