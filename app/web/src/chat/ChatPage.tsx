import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { renderFragment, type LinkTargets } from '../doc/buildDoc';
import { createChat, editMessage, interruptChat, openChatEvents, sendMessage } from './api';
import {
  applyEvent, compactText, contextText, contextWarn, editableUuids, fromSnapshot, isBusy, stateText, toolDetail,
  toolSummary, turnDocLinks, usageText, type ChatView,
} from './chatModel';
import { clearDraft, loadDraft, saveDraft } from './drafts';
import type { ChatItem, ChatOptions } from './types';

const INPUT_MAX_PX = 160;

/** One conversation; with `id === null`, the blank page before its first message. */
export function ChatPage({ id, newTicker, options, targets, onBack, onOpenDoc, onCreated }: {
  id: string | null;
  newTicker: string | null;
  options: ChatOptions | null;
  targets: LinkTargets;
  onBack: () => void;
  onOpenDoc: (ticker: string) => void;
  onCreated: (id: string) => void;
}) {
  const draftKey = id ?? `new/${newTicker ?? ''}`;
  const [view, setView] = useState<ChatView | null>(null);
  const [text, setText] = useState(() => loadDraft(draftKey));
  const [editing, setEditing] = useState<string | null>(null); // uuid of the message being rewritten
  const [error, setError] = useState<string | null>(null);
  const [lost, setLost] = useState(false); // the stream dropped and a reconnect is pending
  const [menu, setMenu] = useState(false);
  const [compactOpen, setCompactOpen] = useState(false);
  const [compactHint, setCompactHint] = useState('');
  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('');
  const [stream, setStream] = useState(0); // bumping it reopens the event stream (after an edit)
  const input = useRef<HTMLTextAreaElement>(null);
  const stick = useRef(true); // follow new output only while the reader is at the bottom

  useEffect(() => {
    if (options && !model) {
      setModel(options.default_model);
      setEffort(options.default_effort);
    }
  }, [options, model]);

  useEffect(() => {
    if (id === null) return;
    return openChatEvents(id, {
      snapshot: (s) => {
        setLost(false);
        setView((v) => fromSnapshot(s, v));
      },
      event: (e) => setView((v) => (v ? applyEvent(v, e) : v)),
      reset: () => setStream((n) => n + 1),
      gone: () => {
        setLost(false);
        setError('对话已归档');
      },
      lost: () => setLost(true),
    });
  }, [id, stream]);

  useEffect(() => {
    const onScroll = () => {
      stick.current = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 120;
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);
  useLayoutEffect(() => {
    if (stick.current) window.scrollTo(0, document.documentElement.scrollHeight);
  }, [view]);
  useLayoutEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, INPUT_MAX_PX)}px`;
  }, [text]);

  const items = view?.items ?? [];
  const html = useMemo(
    () => new Map(items.flatMap((it) => (it.kind === 'text' ? [[it.key, renderFragment(it.text, targets)] as const] : []))),
    [items, targets]);
  const editable = useMemo(() => editableUuids(items), [items]);
  const links = useMemo(() => turnDocLinks(items), [items]);
  const busy = view ? isBusy(view.conv.state) : false;

  const onText = (t: string) => {
    setText(t);
    saveDraft(draftKey, t);
  };
  const fail = (e: unknown) => setError(String(e instanceof Error ? e.message : e));

  const submit = async () => {
    const body = text.trim();
    if (!body) return;
    setError(null);
    try {
      if (id === null) {
        const created = await createChat({ kind: newTicker ? 'ticker' : 'research', ticker: newTicker, text: body, model, effort });
        clearDraft(draftKey);
        onCreated(created.id);
        return;
      }
      if (editing !== null) {
        await editMessage(id, editing, body);
        setEditing(null);
      } else {
        const localId = `${Date.now()}-${Math.random()}`;
        if (busy) setView((v) => v && { ...v, queued: [...v.queued, { localId, text: body }] });
        try {
          await sendMessage(id, body);
        } catch (e) {
          setView((v) => v && { ...v, queued: v.queued.filter((q) => q.localId !== localId) });
          throw e;
        }
      }
      clearDraft(draftKey);
      setText('');
    } catch (e) {
      fail(e);
    }
  };
  const startEdit = (it: Extract<ChatItem, { kind: 'user' }>) => {
    setEditing(it.uuid);
    onText(it.text);
    input.current?.focus();
  };
  const cancelEdit = () => {
    setEditing(null);
    onText('');
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

  const conv = view?.conv;
  const modelLabel = (m: string) => options?.models.find((x) => x.id === m)?.label ?? m;
  const title = conv?.title ?? (newTicker ? `讨论 ${newTicker}` : '新票调研');
  const ticker = conv?.ticker ?? newTicker;

  return (
    <div className="chat">
      <header className="bar bar-top">
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <div className="chat-title">
            <div className="chat-title-main">{title}</div>
            <div className="chat-meta">
              {ticker && <button className="chat-ticker" onClick={() => onOpenDoc(ticker)}>{ticker}</button>}
              {conv && <span>{modelLabel(conv.model)} · {conv.effort}</span>}
              {view && <span className={contextWarn(view.context) ? 'ctx-warn' : ''}>上下文 {contextText(view.context)}</span>}
              {view?.usage && <span>{usageText(view.usage)}</span>}
            </div>
          </div>
          {id !== null && <button className="icon-btn" onClick={() => setMenu(!menu)} aria-label="更多">⋯</button>}
        </div>
        {menu && (
          <div className="chat-menu">
            <button disabled={busy} onClick={() => { setMenu(false); setCompactOpen(true); }}>压缩上下文</button>
          </div>
        )}
      </header>

      <main className="chat-body">
        {error && <div className="banner-error">{error}</div>}
        {lost && <div className="banner-muted">连接中断，正在重连…</div>}
        {items.map((it, n) => (
          <Fragment key={it.key}>
            {it.kind === 'user' && (
              <div className="msg msg-user">
                <div className="msg-text">{it.text}</div>
                {editable.has(it.uuid) && (
                  <button className="edit-btn" disabled={busy} onClick={() => startEdit(it)} aria-label="编辑">✎</button>
                )}
              </div>
            )}
            {it.kind === 'text' && <div className="msg msg-agent md" dangerouslySetInnerHTML={{ __html: html.get(it.key) ?? '' }} />}
            {it.kind === 'tool' && (
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
        {view?.partial && <div className="msg msg-agent msg-partial">{view.partial}▌</div>}
        {view?.queued.map((q) => (
          <div key={q.localId} className="msg msg-user msg-queued"><div className="msg-text">{q.text}</div><span>排队中</span></div>
        ))}
      </main>

      <footer className="chat-input">
        {editing !== null && (
          <div className="chat-editing">编辑消息：发送后将截断此后的对话（文件改动不会撤销）<button onClick={cancelEdit}>取消</button></div>
        )}
        {id === null && options && (
          <div className="model-picker">
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {options.models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
            </select>
            <select value={effort} onChange={(e) => setEffort(e.target.value)}>
              {options.efforts.map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
          </div>
        )}
        {view && <div className="chat-status">{stateText(view.conv.state)}</div>}
        <div className="chat-input-row">
          <textarea ref={input} rows={1} value={text} onChange={(e) => onText(e.target.value)}
            placeholder={id === null ? (newTicker ? `围绕 ${newTicker} 说点什么` : '想调研哪只票？') : '输入消息'} />
          <button className="send-btn" onClick={submit} disabled={!text.trim() || (editing !== null && busy)}>发送</button>
          {busy && id !== null && <button className="stop-btn" onClick={() => interruptChat(id).catch(fail)} aria-label="停止">■</button>}
        </div>
      </footer>

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
