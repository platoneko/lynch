import { AlertRow } from '../alerts/AlertRow';
import { doneTag } from '../alerts/model';
import { loadDraft, saveDraft } from '../chat/drafts';
import { anomalyPrompt, withPrompt } from '../earnings/prompts';
import { hitText, isPending } from './model';
import type { AnomalyItem, AnomalyList } from './types';

/** Pool tickers whose price moved far from its base close, newest event per ticker, each a tap from research. */
export function AnomaliesPage({ list, error, label, onBack, onResearch, onIgnore, onUnignore, onOpenDoc }: {
  list: AnomalyList | null;
  error: string | null; // the page's own request failed
  label: (ticker: string) => string;
  onBack: () => void;
  onResearch: (ticker: string) => void; // opens the ticker's new 讨论 page, whose draft was just written
  onIgnore: (it: AnomalyItem) => void;
  onUnignore: (it: AnomalyItem) => void;
  onOpenDoc: (ticker: string) => void;
}) {
  const research = (it: AnomalyItem) => {
    const key = `new/${it.ticker}`; // ChatPage's draft key for a new 讨论 chat on this ticker
    saveDraft(key, withPrompt(loadDraft(key), anomalyPrompt(it)));
    onResearch(it.ticker);
  };
  return (
    <div className="chats">
      <header className="bar bar-top">
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <div className="chat-title-main">异动</div>
        </div>
      </header>
      <main className="chats-body">
        {error && <div className="banner-error">{error}</div>}
        {list?.error && <div className="banner-error">{list.error}；登录过期时在终端运行 longbridge auth login</div>}
        {list?.items.map((it) => (
          <AlertRow key={it.ticker} title={label(it.ticker)} sub={hitText(it)} done={doneTag(it)}
            pending={isPending(it)} canRestore={it.ignored && !it.researched}
            onOpen={() => onOpenDoc(it.ticker)} onResearch={() => research(it)}
            onIgnore={() => onIgnore(it)} onRestore={() => onUnignore(it)} />
        ))}
        {list && list.items.length === 0 && <p className="empty">近 7 天没有异动</p>}
        {!list && !error && <p className="empty">加载中…</p>}
      </main>
    </div>
  );
}
