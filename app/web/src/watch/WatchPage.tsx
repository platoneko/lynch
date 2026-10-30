import { useState } from 'react';
import { AlertRow } from '../alerts/AlertRow';
import { ConfirmSheet, type Confirm } from '../chat/ConfirmSheet';
import { loadDraft, saveDraft } from '../chat/drafts';
import { watchPrompt, withPrompt } from '../earnings/prompts';
import { rowSub } from './model';
import type { WatchItem, WatchList } from './types';

/** Tickers whose framework update waits on later data: due ones on top, each a tap from a research chat.
 * A card leaves only by the user's ×. */
export function WatchPage({ list, error, label, onBack, onResearch, onRemove, onOpenDoc }: {
  list: WatchList | null;
  error: string | null; // the page's own request failed
  label: (ticker: string) => string;
  onBack: () => void;
  onResearch: (ticker: string) => void; // opens the ticker's new 讨论 page, whose draft was just written
  onRemove: (ticker: string) => void;
  onOpenDoc: (ticker: string) => void;
}) {
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const due = list?.items.filter((it) => it.due) ?? [];
  const waiting = list?.items.filter((it) => !it.due) ?? [];
  const research = (it: WatchItem) => {
    const key = `new/${it.ticker}`; // ChatPage's draft key for a new 讨论 chat on this ticker
    saveDraft(key, withPrompt(loadDraft(key), watchPrompt(it)));
    onResearch(it.ticker);
  };
  // No 恢复 for a removed card: ask first.
  const remove = (it: WatchItem) =>
    setConfirm({ message: `把 ${label(it.ticker)} 移出观察区？移出后不能恢复。`, label: '移出', onConfirm: () => onRemove(it.ticker) });

  const row = (it: WatchItem) => (
    <AlertRow key={it.ticker} title={label(it.ticker)} sub={rowSub(it)} done={it.researched ? '已调研' : null}
      pending canRestore={false}
      onOpen={() => onOpenDoc(it.ticker)} onResearch={() => research(it)}
      onIgnore={() => remove(it)} onRestore={() => {}} dismissLabel="移出" />
  );

  return (
    <div className="chats">
      <header className="bar bar-top">
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <div className="chat-title-main">观察</div>
        </div>
      </header>
      <main className="chats-body">
        {error && <div className="banner-error">{error}</div>}
        {due.length > 0 && (
          <section>
            <h3 className="chats-caption">已到期</h3>
            {due.map(row)}
          </section>
        )}
        {waiting.length > 0 && (
          <section>
            <h3 className="chats-caption">等待中</h3>
            {waiting.map(row)}
          </section>
        )}
        {list && list.items.length === 0 && <p className="empty">观察区是空的：讨论对话建议先等时，勾选卡片加入</p>}
        {!list && !error && <p className="empty">加载中…</p>}
      </main>
      <ConfirmSheet confirm={confirm} onClose={() => setConfirm(null)} />
    </div>
  );
}
