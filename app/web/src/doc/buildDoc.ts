import katex from 'katex';
import MarkdownIt from 'markdown-it';
import texmath from 'markdown-it-texmath';
import { docHash } from '../route';

export interface DocSection {
  id: string;
  index: number; // position in the flat h2/h3 order the jump bars walk
  level: 2 | 3;
  title: string;
  parentId: string | null; // the enclosing h2 for an h3
  bodyHtml: string; // content up to the next h2/h3, children excluded
}

export interface BuiltDoc {
  headerHtml: string; // title + header blockquote, always shown
  sections: DocSection[];
}

type Token = ReturnType<MarkdownIt['parse']>[number];
interface RenderEnv {
  pool: ReadonlySet<string>;
  linkIsInternal: boolean[]; // pairs each link_close with its link_open
}

const md = createMarkdown();

function createMarkdown(): MarkdownIt {
  // Frameworks never need raw HTML; keep it escaped.
  const m = new MarkdownIt({ html: false });
  // Math must be claimed before markdown sees `\(` as an escaped paren.
  // `strict: 'ignore'`: CJK words inside math are deliberate and render fine; only the warnings go.
  m.use(texmath, {
    engine: { renderToString },
    delimiters: 'brackets',
    katexOptions: { throwOnError: false, strict: 'ignore' },
  });
  m.renderer.rules.link_open = (tokens, idx, _opts, env: RenderEnv) => {
    const ticker = linkedTicker(tokens[idx].attrGet('href') ?? '', env.pool);
    env.linkIsInternal.push(ticker !== null);
    return ticker === null ? '<span class="plain-link">' : `<a href="${docHash(ticker)}">`;
  };
  m.renderer.rules.link_close = (_tokens, _idx, _opts, env: RenderEnv) =>
    env.linkIsInternal.pop() ? '</a>' : '</span>';
  m.core.ruler.push('table_layout', (state) => {
    state.tokens.forEach((t, i) => t.type === 'table_open' && layoutTable(state.tokens, i));
  });
  m.renderer.rules.table_open = (tokens, idx, opts, _env, self) =>
    `<div class="table-scroll">${self.renderToken(tokens, idx, opts)}`;
  m.renderer.rules.table_close = () => '</table></div>\n';
  // A prose cell carries its column header, shown only when the phone stacks rows into cards;
  // a folded cell uses that header as its tap-to-expand summary instead.
  m.renderer.rules.td_open = (tokens, idx, opts, env, self) => {
    const td = self.renderToken(tokens, idx, opts);
    const cell = tokens[idx].meta as ProseCell | null;
    if (!cell) return td;
    const label = self.renderInline(cell.header?.children ?? [], opts, env);
    return cell.fold
      ? `${td}<details class="cell-fold"><summary>${label}</summary>`
      : `${td}<span class="cell-label">${label}</span>`;
  };
  m.renderer.rules.td_close = (tokens, idx, opts, _env, self) =>
    ((tokens[idx].meta as ProseCell | null)?.fold ? '</details>' : '') + self.renderToken(tokens, idx, opts);
  return m;
}

// A table with any cell longer than this is prose (Assumptions, Inputs, zone scenarios): kept on
// one line it would run thousands of pixels wide, so the phone shows its rows as cards instead.
// Tables of short cells stay a scrollable grid, where comparing down a column matters.
const PROSE_CELL_CHARS = 40;
// In a card, the first prose column is the row's subject and always shows; later cells this long
// (evidence, impact) fold behind their header.
const FOLD_CELL_CHARS = 80;

interface ProseCell {
  header: Token | undefined; // the column's header inline
  fold: boolean;
}

/** Marks a prose table and annotates its body cells (on td_open and td_close) for the renderer. */
function layoutTable(tokens: Token[], open: number): void {
  // Step 1: collect the header inlines and each body cell as its td_open index.
  const headers: Token[] = [];
  const rows: number[][] = [];
  for (let i = open + 1; tokens[i].type !== 'table_close'; i++) {
    if (tokens[i].type === 'th_open') headers.push(tokens[i + 1]);
    if (tokens[i].type !== 'td_open') continue;
    if (tokens[i - 1].type === 'tr_open') rows.push([]);
    rows[rows.length - 1].push(i);
  }
  const length = (tdOpen: number) => tokens[tdOpen + 1].content.length;

  // Step 2: the subject column is the first one holding a prose cell; none means a grid.
  const subject = Math.min(
    ...rows.flatMap((cells) => cells.flatMap((td, col) => (length(td) > PROSE_CELL_CHARS ? [col] : []))),
  );
  if (subject === Infinity) return;

  // Step 3: annotate. Token layout per cell is td_open, inline, td_close.
  tokens[open].attrJoin('class', 'table-prose');
  for (const cells of rows) {
    cells.forEach((td, col) => {
      const cell: ProseCell = { header: headers[col], fold: col > subject && length(td) > FOLD_CELL_CHARS };
      tokens[td].meta = cell;
      tokens[td + 2].meta = cell;
      if (length(td) === 0) tokens[td].attrJoin('class', 'cell-empty');
    });
  }
}

// Frameworks name code identifiers inside `\text{…}` with a bare `_` (e.g. `\text{perpetual_pe}`),
// which KaTeX rejects in text mode; read it as the literal underscore the author meant.
function renderToString(tex: string, options: katex.KatexOptions): string {
  const fixed = tex.replace(/\\text\{[^{}]*\}/g, (text) => text.replace(/(?<!\\)_/g, '\\_'));
  return katex.renderToString(fixed, options);
}

/** `./NVDA.US.md` or `NVDA.US.md#x` → `NVDA.US`, only for pool members. */
export function linkedTicker(href: string, pool: ReadonlySet<string>): string | null {
  const m = /^(?:\.\/)?([^/#?:]+)\.md(?:#.*)?$/.exec(href);
  return m && pool.has(m[1]) ? m[1] : null;
}

export function buildDoc(source: string, pool: ReadonlySet<string>): BuiltDoc {
  const env: RenderEnv = { pool, linkIsInternal: [] };
  const tokens = md.parse(source, env);
  const render = (from: number, to: number) => md.renderer.render(tokens.slice(from, to), md.options, env);

  // Step 1: find the section boundaries (h2/h3 openings).
  const starts = tokens.flatMap((t, i) =>
    t.type === 'heading_open' && (t.tag === 'h2' || t.tag === 'h3') ? [i] : [],
  );

  // Step 2: slice the token stream at those boundaries; each heading is open/inline/close.
  let currentH2: string | null = null;
  const sections = starts.map((start, i): DocSection => {
    const level = tokens[start].tag === 'h2' ? 2 : 3;
    const id = `s${i}`;
    if (level === 2) currentH2 = id;
    const end = i + 1 < starts.length ? starts[i + 1] : tokens.length;
    return {
      id,
      index: i,
      level,
      title: headingText(tokens[start + 1]),
      parentId: level === 3 ? currentH2 : null,
      bodyHtml: render(start + 3, end),
    };
  });

  return { headerHtml: render(0, starts[0] ?? tokens.length), sections };
}

/** A fragment lifted out of a framework (e.g. an Assumptions cell), rendered like the document body. */
export function renderFragment(source: string, pool: ReadonlySet<string>): string {
  const env: RenderEnv = { pool, linkIsInternal: [] };
  return md.render(source, env);
}

function headingText(inline: Token): string {
  return (inline.children ?? [])
    .map((c) => (c.type === 'softbreak' ? ' ' : c.content))
    .join('')
    .trim();
}

/** Bar text: "§3 可演算块" for an h2, "§3 › Inputs" for an h3. */
export function sectionLabel(s: DocSection, sections: DocSection[]): string {
  const chapter = /^(\d+)\.\s*(.*)$/;
  if (s.parentId === null) {
    const m = chapter.exec(s.title);
    return m ? `§${m[1]} ${m[2]}` : s.title;
  }
  const parent = sections.find((p) => p.id === s.parentId);
  const m = parent ? chapter.exec(parent.title) : null;
  return `${m ? `§${m[1]}` : parent?.title ?? ''} › ${s.title}`;
}
