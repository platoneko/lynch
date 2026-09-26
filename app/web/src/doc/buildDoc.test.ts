import { describe, expect, it } from 'vitest';
import { buildDoc, linkedTicker, sectionLabel } from './buildDoc';

const SAMPLE = [
  '# AAA.US — Alpha', '', '> 林奇：`stalwart`', '',
  '## 0. 现行结论', '', 'intro \\(L = 10\\) text', '',
  '## 3. 可演算块', '', 'calc intro', '',
  '### Inputs', '', '| a | b |', '|---|---|', '| \\(x_1\\) | 2 |', '',
  '### Outputs — 锚点', '', 'see [BBB](./BBB.US.md) and [axis](axes/rates.md)', '',
  '\\[', 'L = M - 1', '\\]', '',
  '## 4. Changelog', '', '### 2026-09-20', '', 'entry', '',
].join('\n');
const POOL = new Set(['AAA.US', 'BBB.US']);

describe('buildDoc', () => {
  const doc = buildDoc(SAMPLE, POOL);

  it('lists h2 and h3 in document order', () => {
    expect(doc.sections.map((s) => [s.title, s.level, s.parentId])).toEqual([
      ['0. 现行结论', 2, null],
      ['3. 可演算块', 2, null],
      ['Inputs', 3, 's1'],
      ['Outputs — 锚点', 3, 's1'],
      ['4. Changelog', 2, null],
      ['2026-09-20', 3, 's4'],
    ]);
    expect(doc.sections.map((s) => s.index)).toEqual([0, 1, 2, 3, 4, 5]);
  });

  it('keeps the title block as the always-open header', () => {
    expect(doc.headerHtml).toContain('AAA.US — Alpha');
    expect(doc.headerHtml).toContain('<blockquote>');
    expect(doc.headerHtml).not.toContain('现行结论');
  });

  it('renders math before markdown eats the backslash', () => {
    expect(doc.sections[0].bodyHtml).toContain('class="katex');
    expect(doc.sections[0].bodyHtml).not.toContain('\\(');
    expect(doc.sections[3].bodyHtml).toContain('katex-display');
  });

  it('reads a bare underscore inside \\text as a literal', () => {
    const html = buildDoc('## 0. x\n\n\\(\\text{perpetual_pe}(g)\\)\n', POOL).sections[0].bodyHtml;
    expect(html).toContain('class="katex');
    expect(html).not.toContain('katex-error');
  });

  it('wraps tables for horizontal scroll and renders math inside cells', () => {
    expect(doc.sections[2].bodyHtml).toContain('<div class="table-scroll"><table>');
    expect(doc.sections[2].bodyHtml).toContain('class="katex');
  });

  it('keeps an h2 intro separate from its h3 children', () => {
    expect(doc.sections[1].bodyHtml).toContain('calc intro');
    expect(doc.sections[1].bodyHtml).not.toContain('Inputs');
  });

  it('turns pool links into in-app links and others into plain text', () => {
    expect(doc.sections[3].bodyHtml).toContain('<a href="#/doc/BBB.US">BBB</a>');
    expect(doc.sections[3].bodyHtml).toContain('<span class="plain-link">axis</span>');
    const isolated = buildDoc(SAMPLE, new Set());
    expect(isolated.sections[3].bodyHtml).not.toContain('<a ');
  });

  it('labels sections for the jump bars', () => {
    expect(sectionLabel(doc.sections[1], doc.sections)).toBe('§3 可演算块');
    expect(sectionLabel(doc.sections[2], doc.sections)).toBe('§3 › Inputs');
    expect(sectionLabel(doc.sections[5], doc.sections)).toBe('§4 › 2026-09-20');
  });
});

describe('linkedTicker', () => {
  it('accepts relative pool links only', () => {
    expect(linkedTicker('./BBB.US.md', POOL)).toBe('BBB.US');
    expect(linkedTicker('BBB.US.md#a2', POOL)).toBe('BBB.US');
    expect(linkedTicker('./CCC.US.md', POOL)).toBeNull();
    expect(linkedTicker('axes/rates.md', POOL)).toBeNull();
    expect(linkedTicker('https://example.com/BBB.US.md', POOL)).toBeNull();
  });
});
