import { useState } from 'react';
import { createPortal } from 'react-dom';
import { isBusy } from './chatModel';
import { ConfirmSheet, type Confirm } from './ConfirmSheet';
import { relativeTime } from './listModel';
import type { ChatSummary } from './types';

/** The doc page's 「讨论」: this ticker's conversations, including the research that landed it. */
export function ChatSheetButton({ ticker, chats, onOpen, onNew }: {
  ticker: string;
  chats: ChatSummary[];
  onOpen: (id: string) => void;
  onNew: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const mine = chats.filter((c) => c.ticker === ticker).sort((a, b) => b.updated_at - a.updated_at);
  const running = mine.some((c) => isBusy(c.state));
  const now = Date.now() / 1000;
  const startNew = () => {
    if (!running) {
      onNew();
      return;
    }
    setConfirm({ message: `${ticker} 已有对话在运行，仍要新开一个？`, label: '新开', onConfirm: onNew });
  };
  return (
    <>
      <button className="text-btn" onClick={() => setOpen(true)}>
        讨论{running && <span className="run-dot">●</span>}
      </button>
      {/* Portaled: inside the fixed top bar's stacking context the bottom bar would paint over the sheet. */}
      {open && createPortal(
        <div className="sheet-backdrop" onClick={() => setOpen(false)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <button className="chat-doc-link" onClick={startNew}>＋ 新对话</button>
            {mine.map((c) => (
              <div key={c.id} className="chat-row">
                <button className="chat-row-main" onClick={() => onOpen(c.id)}>
                  {isBusy(c.state) && <span className="run-dot">●</span>}
                  <span className="chat-row-title">{c.kind === 'research' ? '调研 · ' : ''}「{c.title}」</span>
                  <span className="chat-row-time">{relativeTime(c.updated_at, now)}</span>
                </button>
              </div>
            ))}
            {mine.length === 0 && <p className="empty">这只票还没有对话</p>}
          </div>
        </div>,
        document.body,
      )}
      <ConfirmSheet confirm={confirm} onClose={() => setConfirm(null)} />
    </>
  );
}
