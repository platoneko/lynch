/** One alert row, shared by the earnings and anomaly pages: open the doc, research, dismiss or restore. */
export function AlertRow({ title, sub, done, pending, canRestore, onOpen, onResearch, onIgnore, onRestore }: {
  title: string;
  sub: string;
  done: string | null; // 已调研 / 已忽略, greyed
  pending: boolean; // shows ×
  canRestore: boolean; // shows 恢复
  onOpen: () => void;
  onResearch: () => void;
  onIgnore: () => void;
  onRestore: () => void;
}) {
  return (
    <div className={`cand-row ${done ? 'earn-done' : ''}`}>
      <button className="chat-row-main earn-main" onClick={onOpen}>
        <span className="chat-row-title">{title}</span>
        <span className="earn-sub">{sub}{done ? ` · ${done}` : ''}</span>
      </button>
      <button className="text-btn" onClick={onResearch}>调研</button>
      {pending && <button className="x-btn" aria-label={`忽略 ${title}`} onClick={onIgnore}>×</button>}
      {canRestore && <button className="text-btn" onClick={onRestore}>恢复</button>}
    </div>
  );
}
