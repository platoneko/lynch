import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchCodexOptions } from './api';
import { requestCodexOptions, type CodexCatalogState } from './codexOptions';
import type { ChatOptions } from './types';

const catalog: ChatOptions['codex'] = {
  models: [{ id: 'gpt-research', label: 'GPT research', efforts: ['medium', 'high'], default_effort: 'high' }],
  default_model: 'gpt-research', hint: null,
};

function deferred() {
  let resolve!: (value: ChatOptions['codex']) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<ChatOptions['codex']>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe('lazy Codex catalog', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('requests only the dedicated endpoint when loading the Codex catalog', async () => {
    const paths: string[] = [];
    vi.stubGlobal('fetch', async (path: string) => {
      paths.push(path);
      return new Response(JSON.stringify(catalog), { status: 200 });
    });
    expect(await fetchCodexOptions()).toEqual(catalog);
    expect(paths).toEqual(['/api/chats/codex/options']);
  });

  it('does not request or change state for other research agents', () => {
    const states: CodexCatalogState[] = [];
    let requests = 0;
    for (const agent of ['grok', 'claude', 'opencode'] as const) {
      requestCodexOptions(agent, async () => { requests++; return catalog; }, (state) => states.push(state));
    }
    expect(requests).toBe(0);
    expect(states).toEqual([]);
  });

  it('publishes disabled loading choices immediately, then the returned models', async () => {
    const pending = deferred();
    const states: CodexCatalogState[] = [];
    requestCodexOptions('codex', () => pending.promise, (state) => states.push(state));
    expect(states).toEqual([{ loading: true, options: { models: [], default_model: null, hint: '正在加载 Codex 模型…' } }]);
    pending.resolve(catalog);
    await pending.promise;
    expect(states.at(-1)).toEqual({ loading: false, options: catalog });
  });

  it('keeps an empty response visible with a reason and no usable model', async () => {
    const states: CodexCatalogState[] = [];
    const empty = { models: [], default_model: null, hint: '请先登录 ChatGPT' };
    requestCodexOptions('codex', async () => empty, (state) => states.push(state));
    await Promise.resolve();
    expect(states.at(-1)).toEqual({ loading: false, options: empty });
  });

  it('turns request failures into a visible hint with no stale cached models', async () => {
    const states: CodexCatalogState[] = [];
    requestCodexOptions('codex', async () => { throw new Error('服务暂不可用'); }, (state) => states.push(state));
    await Promise.resolve();
    expect(states.at(-1)).toEqual({ loading: false, options: {
      models: [], default_model: null, hint: 'Codex 模型加载失败：服务暂不可用',
    } });
  });

  it('ignores a late response after switching away from Codex', async () => {
    const pending = deferred();
    const states: CodexCatalogState[] = [];
    const cancel = requestCodexOptions('codex', () => pending.promise, (state) => states.push(state));
    cancel();
    pending.resolve(catalog);
    await pending.promise;
    expect(states).toHaveLength(1);
    expect(states[0].loading).toBe(true);
  });

  it('ignores an older response after switching back and starting a newer request', async () => {
    const old = deferred();
    const latest = deferred();
    let state: CodexCatalogState | null = null;
    const publish = (next: CodexCatalogState) => { state = next; };
    const cancel = requestCodexOptions('codex', () => old.promise, publish);
    cancel();
    requestCodexOptions('codex', () => latest.promise, publish);
    latest.resolve(catalog);
    await latest.promise;
    old.resolve({ models: [], default_model: null, hint: '过期结果' });
    await old.promise;
    expect(state).toEqual({ loading: false, options: catalog });
  });

  it('ignores failures after leaving the page', async () => {
    const pending = deferred();
    const states: CodexCatalogState[] = [];
    const cancel = requestCodexOptions('codex', () => pending.promise, (state) => states.push(state));
    cancel();
    pending.reject(new Error('迟到的错误'));
    await pending.promise.catch(() => {});
    expect(states).toHaveLength(1);
  });
});
