import katex from 'katex';
import MarkdownIt from 'markdown-it';
import texmath from 'markdown-it-texmath';
import { axisHash, docHash } from '../route';
import { ancestorIds } from './nav';

export interface DocSection {
  id: string;
  index: number; // position in the flat order the jump bars walk
  level: 2 | 3 | 4; // 4: a table label inside an h3 (`**表1 · …**`), a jump point that never folds
  title: string;
  parentId: string | null; // the enclosing h2 for an h3, the enclosing h3 for a table label
  bodyHtml: string; // content up to the next section, children excluded
}

export interface BuiltDoc {
  headerHtml: string; // title + header blockquote, always shown
  sections: DocSection[];
}

type Token = ReturnType<MarkdownIt['parse']>[number];
/** What a document may link to in-app: pool tickers and scenario axes (by file stem). */
export interface LinkTargets {
  pool: ReadonlySet<string>;
  axes: ReadonlySet<string>;
}

interface RenderEnv extends LinkTargets {
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
    const href = tokens[idx].attrGet('href') ?? '';
    const ticker = linkedTicker(href, env.pool);
    const axis = ticker === null ? linkedAxis(href, env.axes) : null;
    const target = ticker !== null ? docHash(ticker) : axis !== null ? axisHash(axis) : null;
    env.linkIsInternal.push(target !== null);
    return target === null ? '<span class="plain-link">' : `<a href="${target}">`;
  };
  // Frameworks cite axes by logical id in code spans (`_AXIS.coal-price`); outside a link, that is one.
  const codeInline = m.renderer.rules.code_inline!;
  m.renderer.rules.code_inline = (tokens, idx, opts, env: RenderEnv, self) => {
    const code = codeInline(tokens, idx, opts, env, self);
    const axis = env.linkIsInternal.length === 0 ? citedAxis(tokens[idx].content, env.axes) : null;
    return axis === null ? code : `<a href="${axisHash(axis)}">${code}</a>`;
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

/** `./axes/coal-price.md` → `coal-price`, only for known scenario axes. */
export function linkedAxis(href: string, axes: ReadonlySet<string>): string | null {
  const m = /^(?:\.\/)?axes\/([^/#?:]+)\.md(?:#.*)?$/.exec(href);
  return m && axes.has(m[1]) ? m[1] : null;
}

/** `_AXIS.coal-price` (also with `.md`) → `coal-price`, only for known scenario axes. */
export function citedAxis(code: string, axes: ReadonlySet<string>): string | null {
  const m = /^_AXIS\.([a-z0-9-]+)(?:\.md)?$/.exec(code.trim());
  return m && axes.has(m[1]) ? m[1] : null;
}

export function buildDoc(source: string, targets: LinkTargets): BuiltDoc {
  const env: RenderEnv = { ...targets, linkIsInternal: [] };
  const tokens = md.parse(source, env);
  const render = (from: number, to: number) => md.renderer.render(tokens.slice(from, to), md.options, env);

  // Step 1: find the section boundaries: h2/h3 openings, and table labels inside an h3.
  const boundaries: { start: number; level: 2 | 3 | 4; title: string; bodyFrom: number }[] = [];
  let inH3 = false;
  tokens.forEach((t, i) => {
    if (t.type === 'heading_open' && (t.tag === 'h2' || t.tag === 'h3')) {
      inH3 = t.tag === 'h3';
      // A heading is open/inline/close; its body starts after it.
      boundaries.push({ start: i, level: inH3 ? 3 : 2, title: headingText(tokens[i + 1]), bodyFrom: i + 3 });
      return;
    }
    const label = inH3 ? tableLabel(tokens, i) : null;
    // The label paragraph stays in the body: it is the table's caption.
    if (label !== null) boundaries.push({ start: i, level: 4, title: label, bodyFrom: i });
  });

  // Step 2: slice the token stream at those boundaries; a section's parent is the latest one a level up.
  const latest = new Map<number, string>(); // level → id
  const sections = boundaries.map((b, i): DocSection => {
    const id = `s${i}`;
    latest.set(b.level, id);
    const end = i + 1 < boundaries.length ? boundaries[i + 1].start : tokens.length;
    return {
      id,
      index: i,
      level: b.level,
      title: b.title,
      parentId: b.level === 2 ? null : latest.get(b.level - 1) ?? null,
      bodyHtml: render(b.bodyFrom, end),
    };
  });

  return { headerHtml: render(0, boundaries[0]?.start ?? tokens.length), sections };
}

/**
 * A top-level paragraph opening with `**表2′ · 并列价带（…）**` → `表2′ · 并列价带`; anything else → null.
 * A list item opening that way cites a table (a method step), it does not caption one.
 */
function tableLabel(tokens: Token[], i: number): string | null {
  if (tokens[i].type !== 'paragraph_open' || tokens[i].level !== 0) return null;
  // markdown-it leads a paragraph that opens with emphasis with an empty text token.
  const [open, text] = (tokens[i + 1].children ?? []).filter((c) => !(c.type === 'text' && c.content === ''));
  if (open?.type !== 'strong_open' || text?.type !== 'text' || !/^表\d/.test(text.content)) return null;
  return text.content.split(/[（(]/)[0].trim();
}

/** A fragment lifted out of a framework (e.g. an Assumptions cell), rendered like the document body. */
export function renderFragment(source: string, targets: LinkTargets): string {
  const env: RenderEnv = { ...targets, linkIsInternal: [] };
  return md.render(source, env);
}

function headingText(inline: Token): string {
  return (inline.children ?? [])
    .map((c) => (c.type === 'softbreak' ? ' ' : c.content))
    .join('')
    .trim();
}

/** Bar text: "§3 可演算块" for an h2, "§3 › Inputs" for anything under it. */
export function sectionLabel(s: DocSection, sections: DocSection[]): string {
  const chapter = /^(\d+)\.\s*(.*)$/;
  if (s.parentId === null) {
    const m = chapter.exec(s.title);
    return m ? `§${m[1]} ${m[2]}` : s.title;
  }
  const top = sections.find((p) => p.id === ancestorIds(sections, s.index).at(-1));
  const m = top ? chapter.exec(top.title) : null;
  return `${m ? `§${m[1]}` : top?.title ?? ''} › ${s.title}`;
}
