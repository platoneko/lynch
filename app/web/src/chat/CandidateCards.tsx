import { useState } from 'react';
import { saveCandidates } from './api';
import { bjtTime, candidateLabel, defaultPicked, shownSources, sourceTimeline, STANCE_LABEL } from './candidateModel';
import type { Candidate, CandidateSource } from './types';

/** The candidates one Grok reply ends with. Ticking and tapping 存入候选区 is the user's consent to save them. */
export function CandidateCards({ convId, itemKey, candidates, dropped, hasFramework, saved, onSaved }: {
  convId: string;
  itemKey: string;
  candidates: Candidate[];
  dropped: number;
  hasFramework: (ticker: string) => boolean;
  saved: ReadonlySet<string>; // tickers already in the candidate list (not archived)
  onSaved: (tickers: string[]) => void;
}) {
  const [picked, setPicked] = useState(() => defaultPicked(candidates, hasFramework, saved));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toggle = (t: string) =>
    setPicked((p) => {
      const n = new Set(p);
      if (n.has(t)) n.delete(t);
      else n.add(t);
      return n;
    });
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const { saved: done } = await saveCandidates(convId, itemKey, [...picked]);
      // Saved ones untick; their 已在候选区 badge now marks them.
      setPicked((p) => new Set([...p].filter((t) => !done.includes(t))));
      onSaved(done);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="cand-cards">
      {candidates.map((c) => (
        <div key={c.ticker} className="cand-card">
          <input type="checkbox" aria-label={`选择 ${candidateLabel(c)}`} checked={picked.has(c.ticker)}
            onChange={() => toggle(c.ticker)} />
          <div className="cand-body">
            <div className="cand-head">
              <span className="cand-label">{candidateLabel(c)}</span>
              {hasFramework(c.ticker) && <span className="cand-badge">已有框架</span>}
              {saved.has(c.ticker) && <span className="cand-badge">已在候选区</span>}
            </div>
            <SourceList sources={c.sources} />
          </div>
        </div>
      ))}
      {dropped > 0 && <div className="notice">另有 {dropped} 条格式不合格，已忽略</div>}
      {error && <div className="banner-error">{error}</div>}
      <button className={`text-btn cand-save ${picked.size > 0 ? 'cand-save-on' : ''}`} disabled={busy || picked.size === 0} onClick={save}>
        存入候选区（{picked.size}）
      </button>
    </div>
  );
}

/** A candidate's sources: the shown ones, and every stored one on request, newest first. */
export function SourceList({ sources }: { sources: CandidateSource[] }) {
  const [all, setAll] = useState(false);
  const shown = shownSources(sources);
  return (
    <>
      {(all ? sourceTimeline(sources) : shown).map((s) => <CandidateSourceView key={s.url} source={s} />)}
      {sources.length > shown.length && (
        <button className="text-btn" onClick={() => setAll((a) => !a)}>
          {all ? '收起' : `查看全部 ${sources.length} 条来源`}
        </button>
      )}
    </>
  );
}

export function CandidateSourceView({ source: s }: { source: CandidateSource }) {
  return (
    <div className="cand-src">
      <div className="cand-src-head">@{s.handle} · {STANCE_LABEL[s.stance]} · {bjtTime(s.posted_at)}</div>
      <div>{s.reason}</div>
      <blockquote className="cand-quote">{s.quote}</blockquote>
      <a className="cand-link" href={s.url} target="_blank" rel="noreferrer">原帖</a>
    </div>
  );
}
