import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { fetchAxis, fetchCatalog, fetchFramework } from './api';
import {
  archiveCandidate, archiveChat, archiveTicker, fetchCandidates, fetchChatOptions, fetchChats, restoreCandidate,
} from './chat/api';
import { groupCandidates } from './chat/candidateModel';
import { CandidatesPage } from './chat/CandidatesPage';
import { ChatPage } from './chat/ChatPage';
import { ChatSheetButton } from './chat/ChatSheet';
import { ChatsPage } from './chat/ChatsPage';
import { tickerLabel } from './chat/listModel';
import type { ChatList, ChatOptions, SavedCandidate } from './chat/types';
import { AxisDocHead, DocPage, TickerDocHead } from './doc/DocPage';
import { HomePage } from './home/HomePage';
import { DEFAULT_FILTER, type HomeFilter } from './home/listModel';
import { axisHash, candidatesHash, chatHash, chatsHash, docHash, newChatHash, newDiscoverHash, parseRoute, type Route } from './route';
import type { Catalog } from './types';

export function zoneLabels(catalog: Catalog): Map<string, string> {
  return new Map(catalog.zones.map((z) => [z.id, z.label]));
}

export function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // Filter lives here, not in HomePage, so the query survives a trip into a doc.
  const [filter, setFilter] = useState<HomeFilter>(DEFAULT_FILTER);
  const [chatList, setChatList] = useState<ChatList | null>(null);
  const [chatError, setChatError] = useState<string | null>(null);
  const loadChats = useCallback(() => {
    fetchChats().then((l) => { setChatList(l); setChatError(null); },
      (e: unknown) => setChatError(String(e instanceof Error ? e.message : e)));
  }, []);
  // The home badge and the lists follow agents running on the server; nothing pushes list changes, so poll.
  useEffect(() => {
    if (route.page === 'chat' || route.page === 'chat-new' || route.page === 'chat-new-discover') return;
    loadChats();
    const timer = window.setInterval(loadChats, 10_000);
    return () => window.clearInterval(timer);
  }, [route.page, loadChats]);
  const [candidates, setCandidates] = useState<SavedCandidate[] | null>(null);
  const [candidateError, setCandidateError] = useState<string | null>(null);
  const loadCandidates = useCallback(() => {
    fetchCandidates().then((l) => { setCandidates(l); setCandidateError(null); },
      (e: unknown) => setCandidateError(String(e instanceof Error ? e.message : e)));
  }, []);
  useEffect(() => {
    if (route.page === 'chats' || route.page === 'candidates') loadCandidates();
  }, [route.page, loadCandidates]);
  const [chatOptions, setChatOptions] = useState<ChatOptions | null>(null);
  // A new conversation's first message, handed from its blank page to its own page.
  const [firstMessage, setFirstMessage] = useState<{ id: string; text: string } | null>(null);
  useEffect(() => {
    if (route.page === 'chat' || route.page === 'chat-new' || route.page === 'chat-new-discover') {
      if (!chatOptions) fetchChatOptions().then(setChatOptions, () => {});
    }
  }, [route.page, chatOptions]);
  const names = useMemo(() => new Map((catalog?.tickers ?? []).flatMap((t) => (t.name ? [[t.ticker, t.name] as const] : []))), [catalog]);
  const labels = useMemo(() => new Map((catalog?.tickers ?? []).map((t) => [t.ticker, tickerLabel(t)] as const)), [catalog]);
  const archive = (p: Promise<unknown>) => p.then(loadChats, (e: unknown) => setChatError(String(e instanceof Error ? e.message : e)));
  const homeScroll = useRef(0);
  const openedFromHome = useRef(false);

  const load = useCallback((refresh: boolean) => {
    setLoading(true);
    setError(null);
    fetchCatalog(refresh)
      .then(setCatalog, (e: unknown) => setError(String(e instanceof Error ? e.message : e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => load(false), [load]);
  const reload = () => load(true);

  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useLayoutEffect(() => {
    window.scrollTo(0, route.page === 'home' ? homeScroll.current : 0);
  }, [route]);

  const openFromHome = (hash: string) => {
    homeScroll.current = window.scrollY;
    openedFromHome.current = true;
    window.location.hash = hash;
  };
  const openDoc = (ticker: string) => openFromHome(docHash(ticker));
  const openAxis = (name: string) => openFromHome(axisHash(name));
  // Mirror Android back when we came from the list; a cold deep link has nothing to go back to.
  const goBack = () => {
    if (openedFromHome.current) window.history.back();
    else window.location.hash = '#/';
  };

  const pool = useMemo(() => new Set(catalog?.tickers.map((t) => t.ticker) ?? []), [catalog]);
  const axes = useMemo(() => new Map((catalog?.axes ?? []).map((a) => [a.name, a])), [catalog]);
  // One object per catalog, so a document is not rebuilt on every render.
  const targets = useMemo(() => ({ pool, axes: new Set(axes.keys()) }), [pool, axes]);

  if (!catalog) {
    return (
      <div className="status-screen">
        {error ? (
          <>
            <p>加载失败：{error}</p>
            <button onClick={reload}>重试</button>
          </>
        ) : (
          <p>加载中…</p>
        )}
      </div>
    );
  }
  if (route.page === 'chat' || route.page === 'chat-new' || route.page === 'chat-new-discover') {
    const id = route.page === 'chat' ? route.id : null;
    const newTicker = route.page === 'chat-new' ? route.ticker : null;
    return (
      <ChatPage key={id ?? `new:${route.page}:${newTicker ?? ''}`} id={id}
        newTicker={newTicker} newDiscover={route.page === 'chat-new-discover'}
        firstMessage={firstMessage !== null && firstMessage.id === id ? firstMessage.text : null}
        options={chatOptions} targets={targets}
        onBack={() => (window.history.length > 1 ? window.history.back() : (window.location.hash = chatsHash()))}
        onOpenDoc={(t) => { window.location.hash = docHash(t); }}
        onCreated={(newId, text) => {
          setFirstMessage({ id: newId, text });
          // replace: back from the new conversation must not land on its blank page again
          window.location.replace(chatHash(newId));
        }} />
    );
  }
  if (route.page === 'chats') {
    return (
      <ChatsPage list={chatList} error={chatError} names={names} labels={labels} onBack={goBack}
        onOpen={(id) => { window.location.hash = chatHash(id); }}
        onNewResearch={() => { window.location.hash = newChatHash(null); }}
        onNewDiscover={() => { window.location.hash = newDiscoverHash(); }}
        pendingCandidates={candidates ? groupCandidates(candidates, (t) => pool.has(t)).todo.length : null}
        onOpenCandidates={() => { window.location.hash = candidatesHash(); }}
        onOpenDoc={(t) => { window.location.hash = docHash(t); }}
        onArchive={(id) => archive(archiveChat(id))}
        onArchiveTicker={(t) => archive(archiveTicker(t))} />
    );
  }
  if (route.page === 'candidates') {
    return (
      <CandidatesPage list={candidates} error={candidateError} hasFramework={(t) => pool.has(t)}
        onBack={() => (window.history.length > 1 ? window.history.back() : (window.location.hash = chatsHash()))}
        onResearch={() => { window.location.hash = newChatHash(null); }}
        onArchive={(t) => archiveCandidate(t).then(loadCandidates, (e: unknown) => setCandidateError(String(e instanceof Error ? e.message : e)))}
        onRestore={(t) => restoreCandidate(t).then(loadCandidates, (e: unknown) => setCandidateError(String(e instanceof Error ? e.message : e)))}
        onOpenDoc={(t) => { window.location.hash = docHash(t); }} />
    );
  }
  if (route.page === 'doc') {
    const labels = zoneLabels(catalog);
    const { ticker } = route;
    // From a doc, an axis opens by hash alone: back returns to the doc, not the list.
    const head = (
      <TickerDocHead ticker={ticker} row={catalog.tickers.find((t) => t.ticker === ticker)} targets={targets}
        axes={axes} zoneLabel={(id) => labels.get(id) ?? id} onOpenAxis={(name) => { window.location.hash = axisHash(name); }} />
    );
    return (
      <DocPage key={`doc:${ticker}`} load={() => fetchFramework(ticker)} targets={targets} head={head} onBack={goBack}
        barExtra={<ChatSheetButton ticker={ticker} chats={chatList?.chats ?? []}
          onOpen={(id) => { window.location.hash = chatHash(id); }}
          onNew={() => { window.location.hash = newChatHash(ticker); }} />} />
    );
  }
  if (route.page === 'axis') {
    const { name } = route;
    const head = <AxisDocHead axis={axes.get(name)} pool={pool} />;
    return <DocPage key={`axis:${name}`} load={() => fetchAxis(name)} targets={targets} head={head} onBack={goBack} />;
  }
  return (
    <>
      {error && <div className="banner-error" onClick={reload}>刷新失败：{error}（点此重试）</div>}
      <HomePage catalog={catalog} filter={filter} onFilter={setFilter} onOpen={openDoc} onOpenAxis={openAxis}
        onRefresh={reload} refreshing={loading}
        chatCount={chatList?.running_count ?? 0} onOpenChats={() => openFromHome(chatsHash())} />
    </>
  );
}
