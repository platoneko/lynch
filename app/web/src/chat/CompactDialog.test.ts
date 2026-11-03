import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { CompactDialog } from './CompactDialog';
import { canCompact, compactText } from './chatModel';

describe('manual context compaction', () => {
  it('enables compaction for Codex and Claude while keeping other agents unavailable', () => {
    expect(canCompact({ agent: 'codex' })).toBe(true);
    expect(canCompact({ agent: 'claude' })).toBe(true);
    expect(canCompact({ agent: 'grok' })).toBe(false);
    expect(canCompact({ agent: 'opencode' })).toBe(false);
  });

  it('offers Codex confirmation without an unsupported summary instruction input', () => {
    const html = renderToStaticMarkup(createElement(CompactDialog, {
      agent: 'codex', hint: '', onHint: () => {}, onClose: () => {}, onCompact: () => {},
    }));
    expect(html).toContain('压缩上下文');
    expect(html).toContain('>压缩</button>');
    expect(html).not.toContain('<input');
  });

  it('preserves Claude summary instructions', () => {
    const html = renderToStaticMarkup(createElement(CompactDialog, {
      agent: 'claude', hint: '保留估值假设', onHint: () => {}, onClose: () => {}, onCompact: () => {},
    }));
    expect(html).toContain('<input');
    expect(html).toContain('保留估值假设');
  });

  it('does not invent token counts when the agent reports no post-compaction count', () => {
    expect(compactText({ kind: 'compact', key: 'c1', trigger: 'manual', pre_tokens: 1234, post_tokens: null }))
      .toBe('已压缩');
    expect(compactText({ kind: 'compact', key: 'c2', trigger: 'auto', pre_tokens: null, post_tokens: null }))
      .toBe('已压缩（自动）');
  });
});
