import type { ChatAgent, ChatOptions } from './types';

export interface CodexCatalogState {
  loading: boolean;
  options: ChatOptions['codex'];
}

export const CODEX_LOADING: CodexCatalogState = {
  loading: true, options: { models: [], default_model: null, hint: '正在加载 Codex 模型…' },
};

export function requestCodexOptions(
  agent: ChatAgent,
  load: () => Promise<ChatOptions['codex']>,
  publish: (state: CodexCatalogState) => void,
): () => void {
  if (agent !== 'codex') return () => {};
  let active = true;
  publish(CODEX_LOADING);
  void (async () => {
    try {
      const options = await load();
      if (active) publish({ loading: false, options });
    } catch (e) {
      if (active) publish({ loading: false, options: {
        models: [], default_model: null,
        hint: `Codex 模型加载失败：${e instanceof Error ? e.message : String(e)}`,
      } });
    }
  })();
  return () => { active = false; };
}
