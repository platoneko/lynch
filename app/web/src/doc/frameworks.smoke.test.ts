import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { buildDoc } from './buildDoc';

// Every real framework must render: this is where odd math and table shapes show up.
const DIR = fileURLToPath(new URL('../../../../frameworks/', import.meta.url));
const files = readdirSync(DIR).filter((f) => f.endsWith('.md') && !f.startsWith('_'));
const pool = new Set(files.map((f) => f.slice(0, -'.md'.length)));

describe('real frameworks', () => {
  it('finds the pool', () => {
    expect(files.length).toBeGreaterThan(50);
  });

  it.each(files)('%s renders cleanly', (file) => {
    const doc = buildDoc(readFileSync(join(DIR, file), 'utf8'), pool);
    expect(doc.sections.filter((s) => s.level === 2).map((s) => s.title.split('.')[0])).toEqual(['0', '1', '2', '3', '4']);
    const html = [doc.headerHtml, ...doc.sections.map((s) => s.bodyHtml)].join('\n');
    // Fenced blocks render as `<code class="language-…">`; their literal `\(` is code, not missed math.
    const outsideCode = html.replace(/<code\b[^>]*>[\s\S]*?<\/code>/g, '');
    expect(outsideCode).not.toContain('\\(');
    expect(html).not.toContain('katex-error');
  });
});
