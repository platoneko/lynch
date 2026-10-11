import { loadDraft, saveDraft } from '../chat/drafts';
import { doneTag, isPending, whenText } from './model';
import { earningsPrompt, withPrompt } from './prompts';
import type { EarningsItem, EarningsList } from './types';

/** Pool tickers that just released or are about to release earnings, each a tap away from a research chat. */
export function EarningsPage({ list, error, label, onBack, onResearch, onIgnore, onUnignore, onOpenDoc }: {
  list: EarningsList | null;
  error: string | null; // the page's own request failed
  label: (ticker: string) => string;
  onBack: () => void;
  onResearch: (ticker: string) => void; // opens the ticker's new 讨论 page, whose draft was just written
  onIgnore: (it: EarningsItem) => void;
  onUnignore: (it: EarningsItem) => void;
  onOpenDoc: (ticker: string) => void;
}) {
  const released = list?.items.filter((it) => it.status === 'released') ?? [];
  const upcoming = list?.items.filter((it) => it.status === 'upcoming') ?? [];
  const research = (it: EarningsItem) => {
    const key = `new/${it.ticker}`; // ChatPage's draft key for a new 讨论 chat on this ticker
    saveDraft(key, withPrompt(loadDraft(key), earningsPrompt(it)));
    onResearch(it.ticker);
  };

  const row = (it: EarningsItem) => {
    const tag = doneTag(it);
    return (
      <div key={it.ticker} className={`cand-row ${tag ? 'earn-done' : ''}`}>
        <button className="chat-row-main earn-main" onClick={() => onOpenDoc(it.ticker)}>
          <span className="chat-row-title">{label(it.ticker)}</span>
          <span className="earn-sub">{it.period} · {whenText(it)}{tag ? ` · ${tag}` : ''}</span>
        </button>
        <button className="text-btn" onClick={() => research(it)}>调研</button>
        {isPending(it) && <button className="x-btn" aria-label={`忽略 ${label(it.ticker)}`} onClick={() => onIgnore(it)}>×</button>}
        {it.status === 'released' && it.ignored && !it.researched && (
          <button className="text-btn" onClick={() => onUnignore(it)}>恢复</button>
        )}
      </div>
    );
  };

  return (
    <div className="chats">
      <header className="bar bar-top">
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <div className="chat-title-main">财报</div>
        </div>
      </header>
      <main className="chats-body">
        {error && <div className="banner-error">{error}</div>}
        {list?.error && (
          <div className="banner-error">
            财报日历刷新失败：{list.error}{list.fetched_day ? `（显示的是 ${list.fetched_day} 的数据）` : ''}
            ；登录过期时在终端运行 longbridge auth login
          </div>
        )}
        {released.length > 0 && (
          <section>
            <h3 className="chats-caption">已出</h3>
            {released.map(row)}
          </section>
        )}
        {upcoming.length > 0 && (
          <section>
            <h3 className="chats-caption">即将发布</h3>
            {upcoming.map(row)}
          </section>
        )}
        {list && list.items.length === 0 && <p className="empty">近 7 天没有发布、3 天内也没有要发布的财报</p>}
        {!list && !error && <p className="empty">加载中…</p>}
      </main>
    </div>
  );
}
