import { useState } from 'react';
import { Menu } from '../Menu';
import { isBusy } from './chatModel';
import { ConfirmSheet, type Confirm } from './ConfirmSheet';
import { groupChats, relativeTime, type ChatGroup } from './listModel';
import type { ChatList, ChatSummary } from './types';

/** Every live conversation: unlanded research on top, then one folded group per ticker. */
export function ChatsPage({ list, error, names, labels, onBack, onOpen, onNewResearch, onNewDiscover, pendingCandidates,
  onOpenCandidates, onOpenDoc, onArchive, onArchiveTicker }: {
  list: ChatList | null;
  error: string | null;
  names: ReadonlyMap<string, string>;
  labels: ReadonlyMap<string, string>; // ticker → what the list shows for it (tickerLabel)
  onBack: () => void;
  onOpen: (id: string) => void;
  onNewResearch: () => void;
  onNewDiscover: () => void;
  pendingCandidates: number | null; // null until the candidate list loads
  onOpenCandidates: () => void;
  onOpenDoc: (ticker: string) => void;
  onArchive: (id: string) => void;
  onArchiveTicker: (ticker: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [menu, setMenu] = useState(false);
  const view = groupChats(list?.chats ?? [], query, names);
  const searching = query.trim() !== '';
  const now = Date.now() / 1000;
  const label = (ticker: string) => labels.get(ticker) ?? ticker; // a ticker not in the catalog shows its code
  const toggle = (ticker: string) =>
    setOpen((o) => {
      const n = new Set(o);
      if (n.has(ticker)) n.delete(ticker);
      else n.add(ticker);
      return n;
    });
  const archiveOne = (c: ChatSummary) => {
    const ask = isBusy(c.state) ? `「${c.title}」正在运行，将直接终止并归档。继续？` : `归档「${c.title}」？`;
    setConfirm({ message: ask, label: '归档', onConfirm: () => onArchive(c.id) });
  };
  const archiveGroup = (g: ChatGroup) => {
    const running = g.running ? '其中正在运行的将直接终止。' : '';
    setConfirm({
      message: `归档 ${label(g.ticker)} 的全部 ${g.chats.length} 个对话？${running}`,
      label: '归档',
      onConfirm: () => onArchiveTicker(g.ticker),
    });
  };

  return (
    <div className="chats">
      <header className="bar bar-top">
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <input className="home-search" type="search" placeholder="搜索标的" value={query}
            onChange={(e) => setQuery(e.target.value)} />
          <button className="icon-btn" onClick={() => setMenu(!menu)} aria-label="新建对话">＋</button>
        </div>
        {menu && (
          <Menu onClose={() => setMenu(false)} items={[
            { label: '新票调研', onSelect: onNewResearch },
            { label: '发现新票', onSelect: onNewDiscover },
          ]} />
        )}
      </header>
      <main className="chats-body">
        {error && <div className="banner-error">{error}</div>}
        <button className="chat-doc-link" onClick={onOpenCandidates}>
          候选区{pendingCandidates !== null ? ` · ${pendingCandidates} 只待看` : ''}
        </button>
        {view.discover.length > 0 && (
          <section>
            <h3 className="chats-caption">发现新票</h3>
            {view.discover.map((c) => (
              <ChatRow key={c.id} chat={c} label={`发现新票「${c.title}」`} now={now}
                onOpen={() => onOpen(c.id)} onArchive={() => archiveOne(c)} />
            ))}
          </section>
        )}
        {view.research.length > 0 && (
          <section>
            <h3 className="chats-caption">调研中（未落盘）</h3>
            {view.research.map((c) => (
              <ChatRow key={c.id} chat={c} label={`新票调研「${c.title}」`} now={now}
                onOpen={() => onOpen(c.id)} onArchive={() => archiveOne(c)} />
            ))}
          </section>
        )}
        {view.groups.map((g) => {
          const expanded = searching || open.has(g.ticker);
          return (
            <section key={g.ticker} className="chat-group">
              <div className="chat-group-head">
                <button className="chat-group-toggle" onClick={() => toggle(g.ticker)}>
                  {expanded ? '▾' : '▸'} {g.running && <span className="run-dot">●</span>}
                  {label(g.ticker)} · {g.chats.length} 个对话 · {relativeTime(g.updated_at, now)}
                </button>
                <button className="x-btn" aria-label={`归档 ${label(g.ticker)} 的全部对话`} onClick={() => archiveGroup(g)}>×</button>
              </div>
              {expanded && (
                <>
                  <button className="chat-doc-link" onClick={() => onOpenDoc(g.ticker)}>打开 {label(g.ticker)} 文档</button>
                  {g.chats.map((c) => (
                    <ChatRow key={c.id} chat={c} label={`${c.kind === 'research' ? '调研 · ' : ''}「${c.title}」`} now={now}
                      onOpen={() => onOpen(c.id)} onArchive={() => archiveOne(c)} />
                  ))}
                </>
              )}
            </section>
          );
        })}
        {list && view.discover.length === 0 && view.research.length === 0 && view.groups.length === 0 && (
          <p className="empty">{searching ? '没有匹配的对话' : '还没有对话'}</p>
        )}
      </main>
      <ConfirmSheet confirm={confirm} onClose={() => setConfirm(null)} />
    </div>
  );
}

function ChatRow({ chat, label, now, onOpen, onArchive }: {
  chat: ChatSummary; label: string; now: number; onOpen: () => void; onArchive: () => void;
}) {
  return (
    <div className="chat-row">
      <button className="chat-row-main" onClick={onOpen}>
        {isBusy(chat.state) && <span className="run-dot">●</span>}
        <span className="chat-row-title">{label}</span>
        <span className="chat-row-time">{relativeTime(chat.updated_at, now)}</span>
      </button>
      <button className="x-btn" aria-label="归档" onClick={onArchive}>×</button>
    </div>
  );
}
