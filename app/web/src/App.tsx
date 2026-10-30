import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { AnomaliesPage } from './anomalies/AnomaliesPage';
import { fetchAnomalies, ignoreAnomaly, unignoreAnomaly } from './anomalies/api';
import type { AnomalyList } from './anomalies/types';
import { archiveFramework, fetchAxis, fetchCatalog, fetchFramework, restoreFramework } from './api';
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
import { ArchiveButton } from './doc/ArchiveButton';
import { AxisDocHead, DocPage, TickerDocHead } from './doc/DocPage';
import { fetchEarnings, ignoreEarnings, unignoreEarnings } from './earnings/api';
import { EarningsPage } from './earnings/EarningsPage';
import type { EarningsList } from './earnings/types';
import { useOnForeground } from './foreground';
import { getConnection, useConnection } from './offline/connection';
import { syncOffline } from './offline/sync';
import { HomePage } from './home/HomePage';
import { DEFAULT_FILTER, type HomeFilter } from './home/listModel';
import {
  anomaliesHash, axisHash, candidatesHash, chatHash, chatsHash, docHash, earningsHash, newChatHash, newDiscoverHash,
  parseRoute, watchHash, type Route,
} from './route';
import type { Catalog } from './types';
import { fetchWatch, removeWatch } from './watch/api';
import type { WatchList } from './watch/types';
import { WatchPage } from './watch/WatchPage';

// The server refetches quotes every 5 minutes; the home list, earnings, anomalies and the watch list are re-read at
// that pace.
const ALERT_POLL_MS = 5 * 60_000;
// Offline, the home list is asked for at this pace: the first real answer brings the app back online.
const OFFLINE_PROBE_MS = 30_000;

/** Pages that show anchors from the list: only these ask the server for it again. */
function showsAnchors(route: Route): boolean {
  return route.page === 'home' || route.page === 'doc' || route.page === 'axis';
}

export function zoneLabels(catalog: Catalog): Map<string, string> {
  return new Map(catalog.zones.map((z) => [z.id, z.label]));
}

export function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  // Offline, nothing but the home list and the docs has a copy on the phone: the other reads are not tried.
  const connection = useConnection();
  const { offline } = connection;
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState<string | null>(null);
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
    if (offline || route.page === 'chat' || route.page === 'chat-new' || route.page === 'chat-new-discover') return;
    loadChats();
    const timer = window.setInterval(loadChats, 10_000);
    return () => window.clearInterval(timer);
  }, [route.page, loadChats, offline]);
  const [candidates, setCandidates] = useState<SavedCandidate[] | null>(null);
  const [candidateError, setCandidateError] = useState<string | null>(null);
  const loadCandidates = useCallback(() => {
    fetchCandidates().then((l) => { setCandidates(l); setCandidateError(null); },
      (e: unknown) => setCandidateError(String(e instanceof Error ? e.message : e)));
  }, []);
  useEffect(() => {
    if (!offline && (route.page === 'chats' || route.page === 'candidates')) loadCandidates();
  }, [route.page, loadCandidates, offline]);
  const [earnings, setEarnings] = useState<EarningsList | null>(null);
  const [earningsError, setEarningsError] = useState<string | null>(null);
  const loadEarnings = useCallback(() => {
    fetchEarnings().then((l) => { setEarnings(l); setEarningsError(null); },
      (e: unknown) => setEarningsError(String(e instanceof Error ? e.message : e)));
  }, []);
  // Released / upcoming follow the clock (a release flips at the open or close), so poll; the server answers
  // from its day's calendar cache without calling longbridge.
  useEffect(() => {
    if (offline || (route.page !== 'home' && route.page !== 'earnings')) return;
    loadEarnings();
    const timer = window.setInterval(loadEarnings, ALERT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [route.page, loadEarnings, offline]);
  const dismiss = (p: Promise<unknown>) =>
    p.then(loadEarnings, (e: unknown) => setEarningsError(String(e instanceof Error ? e.message : e)));
  const [anomalies, setAnomalies] = useState<AnomalyList | null>(null);
  const [anomaliesError, setAnomaliesError] = useState<string | null>(null);
  const loadAnomalies = useCallback(() => {
    fetchAnomalies().then((l) => { setAnomalies(l); setAnomaliesError(null); },
      (e: unknown) => setAnomaliesError(String(e instanceof Error ? e.message : e)));
  }, []);
  // Anomalies are judged on the server's 5-minute quote batches; poll at the same pace.
  useEffect(() => {
    if (offline || (route.page !== 'home' && route.page !== 'anomalies')) return;
    loadAnomalies();
    const timer = window.setInterval(loadAnomalies, ALERT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [route.page, loadAnomalies, offline]);
  const dismissAnomaly = (p: Promise<unknown>) =>
    p.then(loadAnomalies, (e: unknown) => setAnomaliesError(String(e instanceof Error ? e.message : e)));
  const [watch, setWatch] = useState<WatchList | null>(null);
  const [watchError, setWatchError] = useState<string | null>(null);
  const loadWatch = useCallback(() => {
    fetchWatch().then((l) => { setWatch(l); setWatchError(null); },
      (e: unknown) => setWatchError(String(e instanceof Error ? e.message : e)));
  }, []);
  // A card falls due at midnight and counts as looked at once a chat on it starts: poll like the alerts.
  useEffect(() => {
    if (offline || (route.page !== 'home' && route.page !== 'watch')) return;
    loadWatch();
    const timer = window.setInterval(loadWatch, ALERT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [route.page, loadWatch, offline]);
  const [chatOptions, setChatOptions] = useState<ChatOptions | null>(null);
  // A new conversation's first message, handed from its blank page to its own page.
  const [firstMessage, setFirstMessage] = useState<{ id: string; text: string } | null>(null);
  useEffect(() => {
    if (route.page === 'chat' || route.page === 'chat-new' || route.page === 'chat-new-discover') {
      if (!chatOptions && !offline) fetchChatOptions().then(setChatOptions, () => {});
    }
  }, [route.page, chatOptions, offline]);
  const names = useMemo(() => new Map((catalog?.tickers ?? []).flatMap((t) => (t.name ? [[t.ticker, t.name] as const] : []))), [catalog]);
  const labels = useMemo(() => new Map((catalog?.tickers ?? []).map((t) => [t.ticker, tickerLabel(t)] as const)), [catalog]);
  const archive = (p: Promise<unknown>) => p.then(loadChats, (e: unknown) => setChatError(String(e instanceof Error ? e.message : e)));
  const homeScroll = useRef(0);
  const openedFromHome = useRef(false);

  // The list's tag: each read asks whether it changed since, and an unchanged list costs an empty 304.
  const catalogTag = useRef<string | null>(null);
  const load = useCallback(() => {
    fetchCatalog(catalogTag.current).then((fresh) => {
      setError(null);
      // The server answered: keep the phone's offline copy in step (at most every few minutes).
      if (!getConnection().offline) syncOffline();
      if (fresh === null) return;
      catalogTag.current = fresh.etag;
      setCatalog(fresh.value);
    }, (e: unknown) => setError(String(e instanceof Error ? e.message : e)));
  }, []);

  // Each visit to a page showing anchors asks again, so a framework edited in a chat is current on the way back.
  // The server refetches quotes every 5 minutes; the home list also asks at that pace while it is on screen.
  useEffect(() => {
    if (showsAnchors(route) || catalogTag.current === null) load();
    if (route.page !== 'home') return;
    const timer = window.setInterval(load, ALERT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [route, load]);
  // Back online (a probe answered, or the worker saw a late answer): read the list the server has now.
  const wasOffline = useRef(offline);
  useEffect(() => {
    if (wasOffline.current && !offline) load();
    wasOffline.current = offline;
  }, [offline, load]);
  useEffect(() => {
    if (!offline) return;
    const timer = window.setInterval(load, OFFLINE_PROBE_MS);
    return () => window.clearInterval(timer);
  }, [offline, load]);
  // Back in the foreground on such a page: edits made meanwhile (a terminal session, a git pull) show without a tap.
  useOnForeground(() => {
    if (showsAnchors(route)) load();
  });

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
            <button onClick={load}>重试</button>
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
  if (route.page === 'earnings') {
    return (
      <EarningsPage list={earnings} error={earningsError} names={names} label={(t) => labels.get(t) ?? t} onBack={goBack}
        onResearch={(t) => { window.location.hash = newChatHash(t); }}
        onIgnore={(it) => dismiss(ignoreEarnings(it.ticker, it.date))}
        onUnignore={(it) => dismiss(unignoreEarnings(it.ticker, it.date))}
        onOpenDoc={(t) => { window.location.hash = docHash(t); }} />
    );
  }
  if (route.page === 'anomalies') {
    return (
      <AnomaliesPage list={anomalies} error={anomaliesError} label={(t) => labels.get(t) ?? t} onBack={goBack}
        onResearch={(t) => { window.location.hash = newChatHash(t); }}
        onIgnore={(it) => dismissAnomaly(ignoreAnomaly(it.ticker, it.ref))}
        onUnignore={(it) => dismissAnomaly(unignoreAnomaly(it.ticker, it.ref))}
        onOpenDoc={(t) => { window.location.hash = docHash(t); }} />
    );
  }
  if (route.page === 'watch') {
    return (
      <WatchPage list={watch} error={watchError} label={(t) => labels.get(t) ?? t} onBack={goBack}
        onResearch={(t) => { window.location.hash = newChatHash(t); }}
        onRemove={(t) => removeWatch(t).then(loadWatch, (e: unknown) => setWatchError(String(e instanceof Error ? e.message : e)))}
        onOpenDoc={(t) => { window.location.hash = docHash(t); }} />
    );
  }
  if (route.page === 'doc') {
    const labels = zoneLabels(catalog);
    const { ticker } = route;
    const row = catalog.tickers.find((t) => t.ticker === ticker);
    // From a doc, an axis opens by hash alone: back returns to the doc, not the list.
    const head = (
      <TickerDocHead ticker={ticker} row={row} targets={targets}
        axes={axes} zoneLabel={(id) => labels.get(id) ?? id} onOpenAxis={(name) => { window.location.hash = axisHash(name); }} />
    );
    return (
      <DocPage key={`doc:${ticker}`} load={(held) => fetchFramework(ticker, held)} targets={targets} head={head} onBack={goBack}
        barExtra={<>
          {offline && <span className="offline-tag">离线</span>}
          {row && <ArchiveButton ticker={ticker} archived={row.archived} disabled={offline}
            onToggle={() => (row.archived ? restoreFramework(ticker) : archiveFramework(ticker)).then(load)} />}
          <ChatSheetButton ticker={ticker} chats={chatList?.chats ?? []} disabled={offline}
            onOpen={(id) => { window.location.hash = chatHash(id); }}
            onNew={() => { window.location.hash = newChatHash(ticker); }} />
        </>} />
    );
  }
  if (route.page === 'axis') {
    const { name } = route;
    const head = <AxisDocHead axis={axes.get(name)} pool={pool} />;
    return <DocPage key={`axis:${name}`} load={(held) => fetchAxis(name, held)} targets={targets} head={head} onBack={goBack}
      barExtra={offline && <span className="offline-tag">离线</span>} />;
  }
  return (
    <>
      {error && <div className="banner-error" onClick={load}>刷新失败：{error}（点此重试）</div>}
      <HomePage catalog={catalog} filter={filter} onFilter={setFilter} onOpen={openDoc} onOpenAxis={openAxis}
        chatCount={chatList?.running_count ?? 0} onOpenChats={() => openFromHome(chatsHash())}
        earnings={earnings} onOpenEarnings={() => openFromHome(earningsHash())}
        anomalies={anomalies} onOpenAnomalies={() => openFromHome(anomaliesHash())}
        watch={watch} onOpenWatch={() => openFromHome(watchHash())} connection={connection} />
    </>
  );
}
