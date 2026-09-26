import type { DocSection } from './buildDoc';

/** `current` is -1 while the reader is still above the first heading. */
export function neighbors(count: number, current: number): { prev: number | null; next: number | null } {
  return {
    prev: current > 0 ? current - 1 : null,
    next: current + 1 < count ? current + 1 : null,
  };
}

/** Headings are given in document order with their viewport-relative top. */
export function currentIndex(tops: { index: number; top: number }[], threshold: number): number {
  let current = -1;
  for (const t of tops) {
    if (t.top > threshold) break;
    current = t.index;
  }
  return current;
}

/** Jumping to a section must also open its h2, or an h3 target stays hidden. */
export function openIdsFor(sections: DocSection[], index: number): string[] {
  const s = sections[index];
  return s.parentId === null ? [s.id] : [s.id, s.parentId];
}

export function isVisible(s: DocSection, open: ReadonlySet<string>): boolean {
  return s.parentId === null || open.has(s.parentId);
}
