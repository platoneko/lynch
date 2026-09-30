/** A hit's [start, end) offsets in the text searched. */
export interface Hit {
  start: number;
  end: number;
}

/** The nth hit inside one section (its heading first, then its body). */
export interface DocMatch {
  section: number;
  nth: number;
}

/** Literal, case-insensitive, non-overlapping. */
export function findHits(text: string, needle: string): Hit[] {
  if (!needle) return [];
  const pattern = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  return Array.from(text.matchAll(pattern), (m) => ({ start: m.index, end: m.index + m[0].length }));
}

/** `counts[i]` is how many hits section i holds. */
export function listMatches(counts: readonly number[]): DocMatch[] {
  return counts.flatMap((count, section) => Array.from({ length: count }, (_, nth) => ({ section, nth })));
}

/**
 * The match a ↑ / ↓ press lands on, wrapping past either end. Before the first press
 * (`cursor` null) the walk starts where the reader is: ↓ at the first match in or after
 * section `current`, ↑ at the last one before it.
 */
export function stepMatch(matches: readonly DocMatch[], cursor: number | null, current: number, dir: 1 | -1): number | null {
  const total = matches.length;
  if (total === 0) return null;
  if (cursor !== null) return (cursor + dir + total) % total;
  const ahead = matches.findIndex((m) => m.section >= current);
  const start = ahead === -1 ? total : ahead;
  return dir === 1 ? start % total : (start - 1 + total) % total;
}

// Not searched: KaTeX glyphs are not the source text; a prose table's column names
// repeat as every card's label (and fold summary), so they would count many times over.
const UNSEARCHED = '.katex, .cell-label, .cell-fold > summary, .table-prose thead, .sec-caret';

interface SearchableText {
  text: string;
  pieces: { node: Text; at: number }[]; // each text node and where it starts in `text`
}

/**
 * The text under `root` a reader would search, joined across inline markup so a word split
 * by bold or a link still matches. Block boundaries need no care: the renderer puts a
 * newline between blocks, which a one-line query never matches.
 */
function searchableText(root: Node): SearchableText {
  const pieces: SearchableText['pieces'] = [];
  let text = '';
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => {
      if (n.nodeType === Node.TEXT_NODE) return NodeFilter.FILTER_ACCEPT;
      if (!(n as Element).matches(UNSEARCHED)) return NodeFilter.FILTER_SKIP;
      text += '\n'; // skipped content must not glue the words around it into a match
      return NodeFilter.FILTER_REJECT;
    },
  });
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    pieces.push({ node: n as Text, at: text.length });
    text += n.nodeValue ?? '';
  }
  return { text, pieces };
}

export function countHits(root: Node, needle: string): number {
  return findHits(searchableText(root).text, needle).length;
}

/** The hits under `root` as live ranges, for highlighting and scrolling to. */
export function hitRanges(root: Node, needle: string): Range[] {
  const { text, pieces } = searchableText(root);
  // The piece holding offset `pos`; an end offset belongs to the piece it closes.
  const locate = (pos: number, isEnd: boolean) => {
    let i = pieces.length - 1;
    while (isEnd ? pieces[i].at >= pos : pieces[i].at > pos) i--;
    return { node: pieces[i].node, offset: pos - pieces[i].at };
  };
  return findHits(text, needle).map((hit) => {
    const start = locate(hit.start, false);
    const end = locate(hit.end, true);
    const range = document.createRange();
    range.setStart(start.node, start.offset);
    range.setEnd(end.node, end.offset);
    return range;
  });
}

/** Parsed HTML that is never shown: a template's content loads no images and runs nothing. */
export function inertFragment(html: string): DocumentFragment {
  const template = document.createElement('template');
  template.innerHTML = html;
  return template.content;
}

/** Highlights the hits (the current one in its own colour); returns the cleanup. */
export function paintHits(all: Range[], current: Range | undefined): () => void {
  // Custom highlights leave the rendered markup alone; a browser without them still jumps, unpainted.
  if (!('highlights' in CSS)) return () => {};
  const currentHighlight = new Highlight(...(current ? [current] : []));
  currentHighlight.priority = 1; // it is also among `all`
  CSS.highlights.set('doc-search', new Highlight(...all));
  CSS.highlights.set('doc-search-current', currentHighlight);
  return () => {
    CSS.highlights.delete('doc-search');
    CSS.highlights.delete('doc-search-current');
  };
}

/** Unfolds any table cell holding the hit, then scrolls it to a third of the way down below the bar. */
export function revealHit(hit: Range, barHeight: number): void {
  for (let d = hit.startContainer.parentElement?.closest('details'); d; d = d.parentElement?.closest('details')) {
    d.open = true;
  }
  const below = window.innerHeight - barHeight;
  window.scrollBy(0, hit.getBoundingClientRect().top - barHeight - below / 3);
}
