import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { renderFragment, type LinkTargets } from '../doc/buildDoc';
import { answerApproval, createChat, editMessage, fetchCandidates, interruptChat, openChatEvents, renameChat, sendMessage } from './api';
import { CandidateCards } from './CandidateCards';
import { costText, DISCOVER_OPENER, discoverModelText, partialText } from './candidateModel';
import {
  applyEvent, compactText, contextText, contextWarn, diffLineClass, editableUuids, formatTokens, fromSnapshot, inputJumps, isBusy,
  stateText, toolDetail, toolSummary, turnDocLinks, usageText, type ChatView, type InputJumps, type Queued,
} from './chatModel';
import { cancelEdit, composer, draftText, putBack, sent, startEdit, typed, type Composer } from './composer';
import { clearDraft, loadDraft, loadShowTools, saveDraft, saveShowTools } from './drafts';
import type { ChatItem, ChatOptions } from './types';

const INPUT_MAX_PX = 160;
const PARK_PX = 8; // gap left between the header and a bubble an input jump lands on
const QUEUED_LABEL: Record<Queued['status'], string> = { behind: '排队中', waiting: '等待回应', unanswered: '未处理' };

/** The user bubbles, the top of the band the sticky header and composer leave visible, and where the jumps lead. */
function inputLayout(header: HTMLElement | null, body: HTMLElement | null, footer: HTMLElement | null) {
  const bubbles = [...(body?.querySelectorAll<HTMLElement>('.msg-user') ?? [])];
  const bandTop = header?.getBoundingClientRect().bottom ?? 0;
  const bandBottom = footer?.getBoundingClientRect().top ?? window.innerHeight;
  const jumps = inputJumps(bubbles.map((b) => b.getBoundingClientRect().top), bandTop, bandBottom);
  return { bubbles, bandTop, jumps };
}

type RenderedText = Map<string, { text: string; html: string }>; // item key → its source and HTML

/**
 * One conversation; with `id === null`, the blank page before its first message (a Grok discover chat when `newDiscover`).
 * `firstMessage`: the text the blank page just created this conversation with, pending until claude replays it.
 */
export function ChatPage({ id, newTicker, newDiscover, firstMessage, options, targets, onBack, onOpenDoc, onCreated }: {
  id: string | null;
  newTicker: string | null;
  newDiscover: boolean; // the blank page of a 发现新票 chat
  firstMessage: string | null;
  options: ChatOptions | null;
  targets: LinkTargets;
  onBack: () => void;
  onOpenDoc: (ticker: string) => void;
  onCreated: (id: string, text: string) => void;
}) {
  const draftKey = id ?? (newDiscover ? 'new-discover/' : `new/${newTicker ?? ''}`);
  const [view, setView] = useState<ChatView | null>(null);
  const [input, setInput] = useState<Composer>(() => composer(loadDraft(draftKey)));
  const [sending, setSending] = useState(false);
  const [creating, setCreating] = useState<string | null>(null); // the blank page's first message, while it is sent
  const inFlight = useRef(false); // state lags a render behind; a quick double tap must not send twice
  const [error, setError] = useState<string | null>(null);
  const [lost, setLost] = useState(false); // the stream dropped and a reconnect is pending
  const [gone, setGone] = useState(false); // archived or cleaned up: nothing more can be sent
  const [menu, setMenu] = useState(false);
  const [showTools, setShowTools] = useState(loadShowTools);
  const [compactOpen, setCompactOpen] = useState(false);
  const [compactHint, setCompactHint] = useState('');
  const [renaming, setRenaming] = useState<string | null>(null); // the title being typed, while the sheet is open
  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('');
  const [ctxWindow, setCtxWindow] = useState(0); // discover: the context window picked on the blank page
  const [lookbackDays, setLookbackDays] = useState(0); // discover: how many days back, picked on the blank page
  // discover: tickers in the candidate list; null until loaded, so the cards' first ticks can skip them
  const [savedTickers, setSavedTickers] = useState<ReadonlySet<string> | null>(null);
  const [stream, setStream] = useState(0); // bumping it reopens the event stream (after an edit)
  const [answering, setAnswering] = useState<ReadonlySet<string>>(new Set()); // approvals whose answer is in flight
  const inputEl = useRef<HTMLTextAreaElement>(null);
  const rendered = useRef<{ targets: LinkTargets; html: RenderedText }>({ targets, html: new Map() });
  const stick = useRef(true); // follow new output only while the reader is at the bottom
  const headerEl = useRef<HTMLElement>(null);
  const bodyEl = useRef<HTMLElement>(null);
  const footerEl = useRef<HTMLElement>(null);
  const [jumps, setJumps] = useState<InputJumps>({ prev: null, last: null });
  const layout = () => inputLayout(headerEl.current, bodyEl.current, footerEl.current);
  const syncJumps = () => {
    const next = layout().jumps;
    setJumps((j) => (j.prev === next.prev && j.last === next.last ? j : next)); // scroll fires often; rerender on change only
  };
  const conv = view?.conv;
  const discover = conv ? conv.kind === 'discover' : newDiscover;
  const discoverModel = options?.discover.models.find((m) => m.id === model);
  const pickDiscoverModel = (id: string) => {
    const m = options?.discover.models.find((x) => x.id === id);
    if (!m) return;
    setModel(m.id);
    if (!m.efforts.includes(effort)) setEffort(m.default_effort);
    if (!m.windows.includes(ctxWindow)) setCtxWindow(m.windows[0]);
  };

  useEffect(() => {
    if (!options || model) return;
    if (!newDiscover) {
      setModel(options.default_model);
      setEffort(options.default_effort);
      return;
    }
    const d = options.discover;
    const m = d.models.find((x) => x.id === d.default_model) ?? d.models[0];
    if (!m) return;
    setModel(m.id);
    setEffort(m.default_effort);
    setCtxWindow(m.windows.includes(d.default_window) ? d.default_window : m.windows[0]);
    setLookbackDays(d.default_lookback_days);
  }, [options, model, newDiscover]);

  useEffect(() => {
    if (id === null || !discover) return;
    fetchCandidates().then(
      (l) => setSavedTickers(new Set(l.filter((c) => c.archived_at === null).map((c) => c.ticker))),
      () => setSavedTickers(new Set()), // no candidate list to compare against: still show the cards
    );
  }, [id, discover]);

  useEffect(() => {
    if (id === null) return;
    return openChatEvents(id, {
      snapshot: (s) => {
        setLost(false);
        const first: Queued[] = firstMessage === null ? [] : [{ localId: 'first', text: firstMessage, status: 'waiting' }];
        setView((v) => fromSnapshot(s, v, first));
      },
      event: (e) => setView((v) => (v ? applyEvent(v, e) : v)),
      reset: () => setStream((n) => n + 1),
      gone: () => {
        setLost(false);
        setGone(true);
        setMenu(false);
        setError('对话已归档或不存在');
      },
      lost: () => setLost(true),
    });
  }, [id, stream, firstMessage]);

  useEffect(() => {
    const onScroll = () => {
      stick.current = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 120;
      syncJumps();
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);
  useLayoutEffect(() => {
    if (stick.current) window.scrollTo(0, document.documentElement.scrollHeight);
  }, [view]);
  useLayoutEffect(syncJumps, [view, showTools]);
  useLayoutEffect(() => {
    const el = inputEl.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, INPUT_MAX_PX)}px`;
  }, [input.text]);
  useEffect(() => saveDraft(draftKey, draftText(input)), [draftKey, input]);

  const items = view?.items ?? [];
  // Every event yields a new `items`; render only text that is new, reusing the rest by key.
  const html = useMemo(() => {
    const prev: RenderedText = rendered.current.targets === targets ? rendered.current.html : new Map();
    const next: RenderedText = new Map();
    for (const it of items) {
      if (it.kind !== 'text') continue;
      const hit = prev.get(it.key);
      next.set(it.key, hit?.text === it.text ? hit : { text: it.text, html: renderFragment(it.text, targets) });
    }
    rendered.current = { targets, html: next };
    return next;
  }, [items, targets]);
  const editable = useMemo(() => editableUuids(items), [items]);
  const links = useMemo(() => turnDocLinks(items), [items]);
  const busy = view ? isBusy(view.conv.state) : false;
  const pending: Queued[] = creating !== null ? [{ localId: 'first', text: creating, status: 'waiting' }] : view?.queued ?? [];

  const { text, editing } = input;
  const fail = (e: unknown) => setError(String(e instanceof Error ? e.message : e));
  const answer = async (requestId: string, allow: boolean) => {
    if (id === null) return;
    setAnswering((s) => new Set(s).add(requestId));
    try {
      await answerApproval(id, requestId, allow); // the card leaves on `approval_done`
    } catch (e) {
      fail(e);
      setAnswering((s) => {
        const next = new Set(s);
        next.delete(requestId);
        return next;
      });
    }
  };

  const submit = async () => {
    // A blank 发现新票 page may send with no text: the opener asks for the default KOL sweep.
    const body = text.trim() || (discover && id === null ? DISCOVER_OPENER : '');
    if (!body || inFlight.current) return;
    inFlight.current = true;
    setSending(true);
    setError(null);
    try {
      if (id === null) {
        setCreating(body);
        const created = await createChat({
          kind: discover ? 'discover' : newTicker ? 'ticker' : 'research', ticker: discover ? null : newTicker,
          text: body, model, effort, ...(discover ? { context_window: ctxWindow, lookback_days: lookbackDays } : {}),
        });
        clearDraft(draftKey);
        onCreated(created.id, body);
        return;
      }
      // An edit's new text, like a message, is replayed only once the model starts answering it.
      const localId = `${Date.now()}-${Math.random()}`;
      const status = busy ? 'behind' : 'waiting';
      setView((v) => v && { ...v, queued: [...v.queued, { localId, text: body, status }] });
      try {
        await (editing !== null ? editMessage(id, editing, body) : sendMessage(id, body));
      } catch (e) {
        setView((v) => v && { ...v, queued: v.queued.filter((q) => q.localId !== localId) });
        throw e;
      }
      setInput((c) => sent(c, body));
    } catch (e) {
      setCreating(null); // kept on success: the bubble stays up until the new conversation's page takes over
      fail(e);
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  };
  const jumpTo = (which: 'prev' | 'last') => {
    const { bubbles, bandTop, jumps: j } = layout();
    const target = which === 'last' ? bubbles.at(-1) : j.prev === null ? undefined : bubbles[j.prev];
    if (target) window.scrollTo(0, window.scrollY + target.getBoundingClientRect().top - bandTop - PARK_PX);
  };
  const putBackUnanswered = (q: Queued) => {
    setView((v) => v && { ...v, queued: v.queued.filter((x) => x.localId !== q.localId) });
    setInput((c) => putBack(c, q.text));
    inputEl.current?.focus();
  };
  const edit = (it: Extract<ChatItem, { kind: 'user' }>) => {
    setInput((c) => startEdit(c, it.uuid, it.text));
    inputEl.current?.focus();
  };
  const compact = async () => {
    setCompactOpen(false);
    if (id === null) return;
    try {
      await sendMessage(id, `/compact ${compactHint}`.trim());
      setCompactHint('');
    } catch (e) {
      fail(e);
    }
  };

  const rename = async () => {
    const title = renaming?.trim();
    setRenaming(null);
    if (id === null || !title) return;
    try {
      await renameChat(id, title); // the new title comes back as a `conv` event
    } catch (e) {
      fail(e);
    }
  };

  const modelLabel = (m: string) => options?.models.find((x) => x.id === m)?.label ?? m;
  const title = conv?.title ?? (id !== null ? '对话' : newDiscover ? '发现新票' : newTicker ? `讨论 ${newTicker}` : '新票调研');
  const ticker = conv?.ticker ?? newTicker;

  return (
    <div className="chat">
      <header ref={headerEl} className="bar bar-top">
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <div className="chat-title">
            <div className="chat-title-main">{title}</div>
            <div className="chat-meta">
              {ticker && <button className="chat-ticker" onClick={() => onOpenDoc(ticker)}>{ticker}</button>}
              {conv && <span>{discover ? discoverModelText(conv) : `${modelLabel(conv.model)} · ${conv.effort}`}</span>}
              {view && <span className={contextWarn(view.context) ? 'ctx-warn' : ''}>ctx {contextText(view.context)}</span>}
              {discover ? conv && costText(conv.cost_usd) && <span>{costText(conv.cost_usd)}</span>
                : view?.usage && <span>{usageText(view.usage)}</span>}
            </div>
          </div>
          {id !== null && <button className="icon-btn" disabled={gone} onClick={() => setMenu(!menu)} aria-label="更多">⋯</button>}
        </div>
        {menu && (
          <>
            <div className="menu-backdrop" onClick={() => setMenu(false)} />
            <div className="chat-menu">
              <button onClick={() => { setMenu(false); setShowTools(!showTools); saveShowTools(!showTools); }}>
                {showTools ? '隐藏调用过程' : '显示调用过程'}
              </button>
              <button onClick={() => { setMenu(false); setRenaming(conv?.title ?? ''); }}>重命名</button>
              {!discover && (
                <button disabled={busy} onClick={() => { setMenu(false); setCompactOpen(true); }}>压缩上下文</button>
              )}
            </div>
          </>
        )}
      </header>

      <main ref={bodyEl} className="chat-body">
        {error && <div className="banner-error">{error}</div>}
        {lost && <div className="banner-muted">连接中断，正在重连…</div>}
        {items.map((it, n) => (
          <Fragment key={it.key}>
            {it.kind === 'user' && (
              <div className="msg msg-user">
                <div className="msg-text">{it.text}</div>
                {editable.has(it.uuid) && !discover && (
                  <button className="edit-btn" disabled={busy || gone || sending} onClick={() => edit(it)} aria-label="编辑">✎</button>
                )}
              </div>
            )}
            {it.kind === 'text' && <div className="msg msg-agent md" dangerouslySetInnerHTML={{ __html: html.get(it.key)?.html ?? '' }} />}
            {it.kind === 'text' && it.candidates && it.candidates.length > 0 && id !== null && savedTickers && (
              <CandidateCards convId={id} itemKey={it.key} candidates={it.candidates} dropped={it.candidates_dropped ?? 0}
                hasFramework={(t) => targets.pool.has(t)} saved={savedTickers}
                onSaved={(ts) => setSavedTickers((s) => new Set([...(s ?? []), ...ts]))} />
            )}
            {it.kind === 'tool' && showTools && (
              <details className={`tool ${it.denied ? 'tool-denied' : ''}`}>
                <summary>{toolSummary(it)}</summary>
                <pre>{toolDetail(it)}</pre>
                {it.output !== null && <pre className="tool-output">{it.output}</pre>}
              </details>
            )}
            {it.kind === 'compact' && <div className="compact-line">{compactText(it)}</div>}
            {it.kind === 'notice' && <div className="notice">{it.text}</div>}
            {links.get(n)?.map((t) => (
              <button key={t} className="chat-doc-link" onClick={() => onOpenDoc(t)}>打开 {t} 文档</button>
            ))}
          </Fragment>
        ))}
        {view?.partial && (() => {
          const p = partialText(view.partial);
          return (
            <>
              {p.text && <div className="msg msg-agent msg-partial">{p.text}{!p.collecting && '▌'}</div>}
              {p.collecting && <div className="notice">正在整理候选…</div>}
            </>
          );
        })()}
        {view?.approvals.map((a) => (
          <div key={a.request_id} className="approval">
            <div className="approval-title">修改 skill：{a.path.replace('frameworks/.claude/skills/', '')}</div>
            <pre className="approval-diff">
              {a.diff.split('\n').map((line, n) => <div key={n} className={diffLineClass(line)}>{line || ' '}</div>)}
            </pre>
            <div className="approval-actions">
              <button className="text-btn" disabled={answering.has(a.request_id) || gone} onClick={() => answer(a.request_id, false)}>拒绝</button>
              <button className="text-btn approval-allow" disabled={answering.has(a.request_id) || gone} onClick={() => answer(a.request_id, true)}>允许</button>
            </div>
          </div>
        ))}
        {pending.map((q) => (
          <div key={q.localId} className="msg msg-user msg-queued">
            <div className="msg-text">{q.text}</div>
            <span>{QUEUED_LABEL[q.status]}</span>
            {q.status === 'unanswered' && (
              <button className="text-btn" disabled={editing !== null} onClick={() => putBackUnanswered(q)}>放回输入框</button>
            )}
          </div>
        ))}
      </main>

      <footer ref={footerEl} className="chat-input">
        {(jumps.prev !== null || jumps.last !== null) && (
          <div className="input-jumps">
            {jumps.prev !== null && (
              <button onClick={() => jumpTo('prev')} aria-label="上一条输入" title="上一条输入">
                <svg viewBox="0 0 24 24"><path d="M6 15l6-6 6 6" /></svg>
              </button>
            )}
            {jumps.last !== null && (
              <button onClick={() => jumpTo('last')} aria-label="最后一条输入" title="最后一条输入">
                <svg viewBox="0 0 24 24" className={jumps.last === 'above' ? 'flip' : ''}><path d="M6 7l6 6 6-6M6 13l6 6 6-6" /></svg>
              </button>
            )}
          </div>
        )}
        {editing !== null && (
          <div className="chat-editing">编辑消息：发送后将截断此后的对话（文件改动不会撤销）<button onClick={() => setInput(cancelEdit)}>取消</button></div>
        )}
        {id === null && options && !discover && (
          <div className="model-picker">
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {options.models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
            </select>
            <select value={effort} onChange={(e) => setEffort(e.target.value)}>
              {options.efforts.map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
          </div>
        )}
        {id === null && options && discover && (
          <>
            {options.discover.hint && <div className="banner-muted">{options.discover.hint}</div>}
            {discoverModel && (
              <div className="model-picker">
                <select value={model} onChange={(e) => pickDiscoverModel(e.target.value)}>
                  {options.discover.models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
                </select>
                <select value={ctxWindow} onChange={(e) => setCtxWindow(Number(e.target.value))}>
                  {discoverModel.windows.map((w) => <option key={w} value={w}>{formatTokens(w)}</option>)}
                </select>
                <select value={effort} onChange={(e) => setEffort(e.target.value)}>
                  {discoverModel.efforts.map((x) => <option key={x} value={x}>{x}</option>)}
                </select>
                <select value={lookbackDays} onChange={(e) => setLookbackDays(Number(e.target.value))}>
                  {options.discover.lookback_days.map((d) => <option key={d} value={d}>最近 {d} 天</option>)}
                </select>
              </div>
            )}
          </>
        )}
        {view && <div className="chat-status">{stateText(view.conv.state)}{view.retry && ` · ${view.retry}`}</div>}
        <div className="chat-input-row">
          <textarea ref={inputEl} rows={1} value={text} disabled={gone} onChange={(e) => setInput((c) => typed(c, e.target.value))}
            placeholder={id === null ? (discover ? '想看 KOL 最近推了什么？（直接发送即可）' : newTicker ? `围绕 ${newTicker} 说点什么` : '想调研哪只票？') : '输入消息'} />
          <button className="send-btn" onClick={submit}
            disabled={sending || gone || (editing !== null && busy) || (discover && busy)
              || (discover && id === null ? !discoverModel : !text.trim())}>
            {sending ? '发送中' : discover && busy ? '等这一轮结束' : '发送'}
          </button>
          {busy && id !== null && !gone && <button className="stop-btn" onClick={() => interruptChat(id).catch(fail)} aria-label="停止">■</button>}
        </div>
      </footer>

      {renaming !== null && (
        <div className="sheet-backdrop" onClick={() => setRenaming(null)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <p>重命名对话</p>
            <input className="home-search" autoFocus value={renaming} onChange={(e) => setRenaming(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') rename(); }} />
            <div className="sheet-actions">
              <button className="text-btn" onClick={() => setRenaming(null)}>取消</button>
              <button className="text-btn" disabled={!renaming.trim()} onClick={rename}>保存</button>
            </div>
          </div>
        </div>
      )}

      {compactOpen && (
        <div className="sheet-backdrop" onClick={() => setCompactOpen(false)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <p>压缩上下文：把之前的对话总结成摘要，腾出上下文。</p>
            <input className="home-search" placeholder="可选：摘要要保留什么（如：估值假设和数字）"
              value={compactHint} onChange={(e) => setCompactHint(e.target.value)} />
            <div className="sheet-actions">
              <button className="text-btn" onClick={() => setCompactOpen(false)}>取消</button>
              <button className="text-btn" onClick={compact}>压缩</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
