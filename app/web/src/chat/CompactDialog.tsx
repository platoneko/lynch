import type { ChatAgent } from './types';

export function CompactDialog({ hint, onHint, onClose, onCompact }: {
  agent: ChatAgent;
  hint: string;
  onHint: (hint: string) => void;
  onClose: () => void;
  onCompact: () => void;
}) {
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <p>压缩上下文：把之前的对话总结成摘要，腾出上下文。</p>
        <input className="home-search" placeholder="可选：摘要要保留什么（如：估值假设和数字）"
          value={hint} onChange={(e) => onHint(e.target.value)} />
        <div className="sheet-actions">
          <button className="text-btn" onClick={onClose}>取消</button>
          <button className="text-btn" onClick={onCompact}>压缩</button>
        </div>
      </div>
    </div>
  );
}
