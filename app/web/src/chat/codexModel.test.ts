import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { agentModelText, canStartResearch, chatCostText, isClaude, newChatBody, researchPickerAgent, researchSettings } from './chatModel';
import { ResearchModelPicker } from './ResearchModelPicker';
import { CODEX_LOADING } from './codexOptions';
import type { ChatOptions, ChatSummary } from './types';

const options: ChatOptions = {
  models: [{ id: 'claude-opus-5-5', label: 'Opus 5.5' }],
  efforts: ['medium', 'high'], default_model: 'claude-opus-5-5', default_effort: 'medium',
  agents: [{ id: 'claude', label: 'Claude Code' }, { id: 'codex', label: 'Codex' }, { id: 'opencode', label: 'opencode' }, { id: 'grok', label: 'Grok' }],
  default_agent: 'claude',
  opencode: { models: [{ id: '0g/deepseek-v4.1-flash', label: 'DeepSeek V4.1 Flash' }], default_model: '0g/deepseek-v4.1-flash' },
  codex: {
    models: [
      { id: 'server-model-a', label: 'GPT research', efforts: ['medium', 'high', 'xhigh'], default_effort: 'high' },
      { id: 'server-model-b', label: 'GPT fast', efforts: ['low', 'medium'], default_effort: 'low' },
    ],
    default_model: 'server-model-a', hint: null,
  },
  grok: {
    models: [
      { id: 'grok-research', label: 'Grok research', windows: [256000, 500000], efforts: ['medium', 'high'], default_effort: 'medium' },
      { id: 'grok-fast', label: 'Grok fast', windows: [128000], efforts: ['low'], default_effort: 'low' },
    ],
    default_model: 'grok-research', default_window: 500000, default_effort: 'high', hint: null,
  },
  discover: {
    models: [], default_model: null, default_window: 500000,
    lookback_days: [1, 2, 3, 7], default_lookback_days: 2, hint: '未获取 Grok 模型',
  },
};

describe('Codex model selection', () => {
  it('uses the server Codex defaults when creating a research chat', () => {
    const defaults = { ...options, default_agent: 'codex' as const, codex: {
      models: [...options.codex.models,
        { id: 'gpt-6.1-terra', label: 'GPT 6.1 Terra', efforts: ['medium', 'high', 'xhigh'], default_effort: 'xhigh' }],
      default_model: 'gpt-6.1-terra', hint: null,
    } };
    const picked = researchSettings(defaults, defaults.default_agent);
    expect(canStartResearch(defaults, defaults.default_agent, picked.model, picked.effort)).toBe(true);
    expect(newChatBody({ discover: false, kind: 'research', ticker: null, text: '调研',
      agent: defaults.default_agent, model: picked.model, effort: picked.effort, ctxWindow: 0, lookbackDays: 2,
    })).toEqual({ kind: 'research', ticker: null, text: '调研', agent: 'codex', model: 'gpt-6.1-terra', effort: 'xhigh' });
  });

  it('uses the server model list and the selected model default effort', () => {
    const selection = researchSettings(options, 'codex');
    expect(selection.models.map((m) => m.id)).toEqual(['server-model-a', 'server-model-b']);
    expect(selection.model).toBe('server-model-a');
    expect(selection.effort).toBe('high');
    expect(selection.efforts).toEqual(['medium', 'high', 'xhigh']);
  });

  it('normalizes an unsupported effort when switching models and preserves a supported effort', () => {
    expect(researchSettings(options, 'codex', 'server-model-b', 'xhigh').effort).toBe('low');
    expect(researchSettings(options, 'codex', 'server-model-b', 'medium').effort).toBe('medium');
  });

  it('falls back to the first returned model when the default is unavailable', () => {
    const changed = { ...options, codex: { ...options.codex, default_model: 'removed-model' } };
    expect(researchSettings(changed, 'codex').model).toBe('server-model-a');
  });

  it('keeps unavailable Codex selected with its hint and prevents submitting', () => {
    const unavailable = { ...options, codex: { models: [], default_model: null, hint: '请先登录 ChatGPT' } };
    expect(researchSettings(unavailable, 'codex')).toMatchObject({ model: '', effort: '', efforts: [], hint: '请先登录 ChatGPT' });
    expect(canStartResearch(unavailable, 'codex', '', '')).toBe(false);
    expect(canStartResearch(null, 'codex', '', '')).toBe(false);
  });

  it('accepts only a currently advertised model and effort before sending', () => {
    expect(canStartResearch(options, 'codex', 'server-model-a', 'high')).toBe(true);
    expect(canStartResearch(options, 'codex', 'removed-model', 'high')).toBe(false);
    expect(canStartResearch(options, 'codex', 'server-model-b', 'xhigh')).toBe(false);
  });

  it('preserves Claude and opencode defaults', () => {
    expect(researchSettings(options, options.default_agent)).toMatchObject({ model: 'claude-opus-5-5', effort: 'medium' });
    expect(researchSettings(options, 'opencode')).toMatchObject({ model: '0g/deepseek-v4.1-flash', effort: 'default', efforts: [] });
    expect(canStartResearch(options, 'claude', 'claude-opus-5-5', 'medium')).toBe(true);
    expect(canStartResearch(options, 'opencode', '0g/deepseek-v4.1-flash', 'default')).toBe(true);
  });
});

describe('four research agents after the Grok merge', () => {
  it('routes Codex to its own model picker', () => {
    expect(researchPickerAgent('codex')).toBe('codex');
  });

  it('preserves Grok first-paint defaults, context selection and per-model normalization', () => {
    expect(researchSettings(options, 'grok', 'claude-opus-5-5')).toMatchObject({
      model: 'grok-research', effort: 'high', efforts: ['medium', 'high'], windows: [256000, 500000], ctxWindow: 500000,
    });
    expect(researchSettings(options, 'grok', 'grok-research', 'medium', 256000)).toMatchObject({ effort: 'medium', ctxWindow: 256000 });
    expect(researchSettings(options, 'grok', 'grok-fast', 'high', 500000)).toMatchObject({ effort: 'low', ctxWindow: 128000 });
    expect(canStartResearch(options, 'grok', '', '')).toBe(true);
  });

  it('keeps unavailable Grok separate from Claude and prevents submitting', () => {
    const unavailable = { ...options, grok: { ...options.grok, models: [], default_model: null, hint: '未获取 Grok 调研模型' } };
    expect(researchSettings(unavailable, 'grok')).toMatchObject({ model: '', effort: '', models: [], hint: '未获取 Grok 调研模型' });
    expect(canStartResearch(unavailable, 'grok', 'claude-opus-5-5', 'medium')).toBe(false);
  });

  it('keeps Grok context and lookback fields out of Codex chat requests', () => {
    expect(newChatBody({ discover: false, kind: 'ticker', ticker: 'EXAMPLE.US', text: '调研',
      model: 'server-model-a', effort: 'high', agent: 'codex', ctxWindow: 500000, lookbackDays: 2 })).toEqual({
      kind: 'ticker', ticker: 'EXAMPLE.US', text: '调研', model: 'server-model-a', effort: 'high', agent: 'codex',
    });
  });
});

const codex: ChatSummary = {
  id: 'c1', kind: 'research', ticker: null, title: '调研', model: 'server-model-a', effort: 'high',
  created_at: 1, updated_at: 1, context_window: 400000, archived: false, state: 'idle',
  agent: 'codex', agent_label: 'Codex', model_label: 'GPT research', codex_thread: 'thread-1', cost_usd: 3.5,
};

describe('Codex chat metadata', () => {
  it('shows the server labels while using non-Claude capabilities', () => {
    expect(agentModelText(codex)).toBe('Codex · GPT research · high');
    expect(isClaude(codex)).toBe(false);
  });

  it('does not show USD pricing for Codex even if cost metadata is present', () => {
    expect(chatCostText(codex)).toBe('');
    expect(chatCostText({ ...codex, agent: 'claude' })).toBe('');
    expect(chatCostText({ ...codex, agent: 'grok' })).toBe('本对话 $3.50');
    expect(chatCostText({ ...codex, agent: 'opencode' })).toBe('本对话 $3.50');
    expect(chatCostText({ ...codex, agent: 'opencode', cost_usd: 0 })).toBe('');
  });
});

describe('Codex model picker', () => {
  const render = (available = options, model = 'server-model-a', effort = 'high') => renderToStaticMarkup(createElement(
    ResearchModelPicker, { options: available, agent: 'codex', model, effort, onAgent: () => {}, onModel: () => {}, onEffort: () => {} },
  ));

  it('renders dynamically supplied Codex models and the selected model effort levels', () => {
    const html = render();
    const models = html.match(/<select aria-label="模型"[^>]*>(.*?)<\/select>/)?.[1];
    const efforts = html.match(/<select aria-label="思考强度"[^>]*>(.*?)<\/select>/)?.[1];
    expect(models).toContain('value="server-model-a" selected=""');
    expect(models).toContain('GPT research');
    expect(models).toContain('GPT fast');
    expect(models).not.toContain('claude');
    expect(efforts).toContain('value="xhigh"');
    expect(efforts).not.toContain('value="low"');
  });

  it('shows the unavailable-model hint and disables the empty model picker', () => {
    const html = render({ ...options, codex: { models: [], default_model: null, hint: '请先登录 ChatGPT' } }, '', '');
    expect(html).toContain('请先登录 ChatGPT');
    expect(html).toMatch(/<select aria-label="模型" disabled=""/);
    expect(html).not.toContain('aria-label="思考强度"');
  });

  it('shows loading and prevents using previously cached model choices while the request runs', () => {
    const loading = { ...options, codex: CODEX_LOADING.options };
    const html = render(loading);
    expect(html).toContain('正在加载 Codex 模型');
    expect(html).toMatch(/<select aria-label="模型" disabled=""/);
    expect(html).not.toContain('GPT research');
    expect(canStartResearch(loading, 'codex', 'server-model-a', 'high')).toBe(false);
  });

  it('renders Grok research context choices and never shows Claude models', () => {
    const html = renderToStaticMarkup(createElement(ResearchModelPicker, {
      options, agent: 'grok', model: '', effort: '', onAgent: () => {}, onModel: () => {}, onEffort: () => {},
    }));
    const models = html.match(/<select aria-label="模型"[^>]*>(.*?)<\/select>/)?.[1];
    expect(models).toContain('value="grok-research" selected=""');
    expect(models).not.toContain('claude');
    expect(html).toContain('aria-label="上下文长度"');
    expect(html).toContain('value="500000" selected=""');
    expect(html).not.toContain('回看范围');
  });
});
