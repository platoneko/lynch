import { useState } from 'react';
import { CandidateSourceView } from './CandidateCards';
import { archiveDaysLeft, candidateDraft, candidateLabel, groupCandidates, shownSources, STANCE_LABEL } from './candidateModel';
import { ConfirmSheet, type Confirm } from './ConfirmSheet';
import { loadDraft, saveDraft } from './drafts';
import { relativeTime } from './listModel';
import type { SavedCandidate } from './types';

const RESEARCH_DRAFT = 'new/'; // ChatPage's draft key for a blank 新票调研 page

/** Tickers the user saved from 发现新票: to-do on top, landed and archived folded below. */
export function CandidatesPage({ list, error, hasFramework, onBack, onResearch, onArchive, onRestore, onOpenDoc }: {
  list: SavedCandidate[] | null;
  error: string | null;
  hasFramework: (ticker: string) => boolean;
  onBack: () => void;
  onResearch: () => void; // opens the blank 新票调研 page, whose draft was just written
  onArchive: (ticker: string) => void;
  onRestore: (ticker: string) => void;
  onOpenDoc: (ticker: string) => void;
}) {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set()); // expanded rows and folded groups
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const g = groupCandidates(list ?? [], hasFramework);
  const now = Date.now() / 1000;
  const toggle = (k: string) =>
    setOpen((o) => {
      const n = new Set(o);
      if (n.has(k)) n.delete(k);
      else n.add(k);
      return n;
    });
  const research = (c: SavedCandidate) => {
    const start = () => {
      saveDraft(RESEARCH_DRAFT, candidateDraft(c));
      onResearch();
    };
    if (loadDraft(RESEARCH_DRAFT).trim()) {
      setConfirm({ message: '新票调研里有没发出的草稿，替换成这只票的线索？', label: '替换', onConfirm: start });
    } else start();
  };

  const row = (c: SavedCandidate, actions: 'todo' | 'landed' | 'archived') => (
    <div key={c.ticker} className="cand-row">
      <button className="chat-row-main" onClick={() => toggle(c.ticker)}>
        <span className="chat-row-title">
          {candidateLabel(c)} · {shownSources(c.sources).map((s) => `@${s.handle} ${STANCE_LABEL[s.stance]}`).join('、')}
        </span>
        <span className="chat-row-time">
          {c.archived_at !== null
            ? `${hasFramework(c.ticker) ? '已入库 · ' : ''}还剩 ${archiveDaysLeft(c.archived_at, now)} 天`
            : relativeTime(c.updated_at, now)}
        </span>
      </button>
      {actions === 'todo' && <button className="text-btn" onClick={() => research(c)}>调研</button>}
      {actions === 'landed' && <button className="text-btn" onClick={() => onOpenDoc(c.ticker)}>文档</button>}
      {actions !== 'archived' && <button className="text-btn" onClick={() => onArchive(c.ticker)}>归档</button>}
      {actions === 'archived' && <button className="text-btn" onClick={() => onRestore(c.ticker)}>恢复</button>}
      {open.has(c.ticker) && (
        <div className="cand-detail">{shownSources(c.sources).map((s) => <CandidateSourceView key={s.url} source={s} />)}</div>
      )}
    </div>
  );
  const folded = (key: string, title: string, rows: SavedCandidate[], actions: 'landed' | 'archived') =>
    rows.length > 0 && (
      <section>
        <button className="chat-group-toggle" onClick={() => toggle(key)}>
          {open.has(key) ? '▾' : '▸'} {title} · {rows.length}
        </button>
        {open.has(key) && rows.map((c) => row(c, actions))}
      </section>
    );

  return (
    <div className="chats">
      <header className="bar bar-top">
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <div className="chat-title-main">候选区</div>
        </div>
      </header>
      <main className="chats-body">
        {error && <div className="banner-error">{error}</div>}
        {g.todo.length > 0 && (
          <section>
            <h3 className="chats-caption">待看</h3>
            {g.todo.map((c) => row(c, 'todo'))}
          </section>
        )}
        {folded('#landed', '已入库', g.landed, 'landed')}
        {folded('#archived', '已归档', g.archived, 'archived')}
        {list && list.length === 0 && <p className="empty">还没有候选：在「发现新票」里勾选后存入</p>}
      </main>
      <ConfirmSheet confirm={confirm} onClose={() => setConfirm(null)} />
    </div>
  );
}
