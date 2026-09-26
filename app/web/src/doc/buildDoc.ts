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
  m.renderer.rules.table_open = () => '<div class="table-scroll"><table>\n';
  m.renderer.rules.table_close = () => '</table></div>\n';
  return m;
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
