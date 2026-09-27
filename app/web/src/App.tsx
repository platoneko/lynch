import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { fetchAxis, fetchCatalog, fetchFramework } from './api';
import { AxisDocHead, DocPage, TickerDocHead } from './doc/DocPage';
import { HomePage } from './home/HomePage';
import { DEFAULT_FILTER, type HomeFilter } from './home/listModel';
import { axisHash, docHash, parseRoute, type Route } from './route';
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
  if (route.page === 'doc') {
    const labels = zoneLabels(catalog);
    const { ticker } = route;
    // From a doc, an axis opens by hash alone: back returns to the doc, not the list.
    const head = (
      <TickerDocHead ticker={ticker} row={catalog.tickers.find((t) => t.ticker === ticker)} targets={targets}
        axes={axes} zoneLabel={(id) => labels.get(id) ?? id} onOpenAxis={(name) => { window.location.hash = axisHash(name); }} />
    );
    return <DocPage key={`doc:${ticker}`} load={() => fetchFramework(ticker)} targets={targets} head={head} onBack={goBack} />;
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
        onRefresh={reload} refreshing={loading} />
    </>
  );
}
