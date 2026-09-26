import { describe, expect, it } from 'vitest';
import type { DocSection } from './buildDoc';
import { currentIndex, isVisible, neighbors, openIdsFor } from './nav';

const sec = (index: number, level: 2 | 3, parentId: string | null): DocSection =>
  ({ id: `s${index}`, index, level, title: `t${index}`, parentId, bodyHtml: '' });
const SECTIONS = [sec(0, 2, null), sec(1, 2, null), sec(2, 3, 's1'), sec(3, 3, 's1'), sec(4, 2, null), sec(5, 3, 's4')];

describe('neighbors', () => {
  it('handles header, first, middle and last', () => {
    expect(neighbors(6, -1)).toEqual({ prev: null, next: 0 });
    expect(neighbors(6, 0)).toEqual({ prev: null, next: 1 });
    expect(neighbors(6, 2)).toEqual({ prev: 1, next: 3 });
    expect(neighbors(6, 5)).toEqual({ prev: 4, next: null });
    expect(neighbors(0, -1)).toEqual({ prev: null, next: null });
  });
});

describe('currentIndex', () => {
  it('is the last heading at or above the threshold', () => {
    expect(currentIndex([{ index: 0, top: -300 }, { index: 1, top: 40 }, { index: 4, top: 500 }], 60)).toBe(1);
  });
  it('is -1 while still reading the header', () => {
    expect(currentIndex([{ index: 0, top: 200 }], 60)).toBe(-1);
  });
});

describe('expansion', () => {
  it('opens a section together with its parent', () => {
    expect(openIdsFor(SECTIONS, 2)).toEqual(['s2', 's1']);
    expect(openIdsFor(SECTIONS, 4)).toEqual(['s4']);
  });
  it('shows h3 only under an open h2', () => {
    expect(isVisible(SECTIONS[2], new Set())).toBe(false);
    expect(isVisible(SECTIONS[2], new Set(['s1']))).toBe(true);
    expect(isVisible(SECTIONS[1], new Set())).toBe(true);
  });
});
