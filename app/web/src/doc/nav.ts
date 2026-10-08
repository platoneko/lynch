import type { DocSection } from './buildDoc';

/**
 * `current` is -1 while the reader is still above the first heading.
 * `up` returns to the start of the section being read, not the one before it.
 */
export function neighbors(count: number, current: number): { up: number | null; next: number | null } {
  return {
    up: current >= 0 ? current : null,
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

/** Jumping to a section must also open its ancestors, or a nested target stays hidden. */
export function openIdsFor(sections: DocSection[], index: number): string[] {
  return [sections[index].id, ...ancestorIds(sections, index)];
}

/** A section shows only while every section above it is open. */
export function isVisible(s: DocSection, open: ReadonlySet<string>, sections: DocSection[]): boolean {
  return ancestorIds(sections, s.index).every((id) => open.has(id));
}

/** Parent first, h2 last. */
export function ancestorIds(sections: DocSection[], index: number): string[] {
  const ids: string[] = [];
  let p = sections[index].parentId;
  while (p !== null) {
    ids.push(p);
    const id = p;
    p = sections.find((s) => s.id === id)!.parentId;
  }
  return ids;
}
