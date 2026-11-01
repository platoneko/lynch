import { formatTokens, researchSettings } from './chatModel';
import type { ChatAgent, ChatOptions } from './types';

export function ResearchModelPicker({ options, agent, model, effort, ctxWindow = 0, onAgent, onModel, onEffort, onContextWindow }: {
  options: ChatOptions;
  agent: ChatAgent;
  model: string;
  effort: string;
  ctxWindow?: number;
  onAgent: (agent: ChatAgent) => void;
  onModel: (id: string) => void;
  onEffort: (effort: string) => void;
  onContextWindow?: (window: number) => void;
}) {
  const settings = researchSettings(options, agent, model, effort, ctxWindow);
  return (
    <>
      {settings.hint && <div className="banner-muted">{settings.hint}</div>}
      <div className="model-picker">
        <select aria-label="对话工具" value={agent} onChange={(e) => onAgent(e.target.value as ChatAgent)}>
          {options.agents.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
        </select>
        <select aria-label="模型" value={agent === 'grok' ? settings.model : model} disabled={!settings.models.length} onChange={(e) => onModel(e.target.value)}>
          {settings.models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
        </select>
        {settings.windows.length > 0 && (
          <select aria-label="上下文长度" value={settings.ctxWindow} onChange={(e) => onContextWindow?.(Number(e.target.value))}>
            {settings.windows.map((w) => <option key={w} value={w}>{formatTokens(w)}</option>)}
          </select>
        )}
        {settings.efforts.length > 0 && (
          <select aria-label="思考强度" value={agent === 'grok' ? settings.effort : effort} onChange={(e) => onEffort(e.target.value)}>
            {settings.efforts.map((x) => <option key={x} value={x}>{x}</option>)}
          </select>
        )}
      </div>
    </>
  );
}
