import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { fetchCatalog } from './api';
import { DocPage } from './doc/DocPage';
import { HomePage } from './home/HomePage';
import { DEFAULT_FILTER, type HomeFilter } from './home/listModel';
import { docHash, parseRoute, type Route } from './route';
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

  const openDoc = (ticker: string) => {
    homeScroll.current = window.scrollY;
    openedFromHome.current = true;
    window.location.hash = docHash(ticker);
  };
  // Mirror Android back when we came from the list; a cold deep link has nothing to go back to.
  const goBack = () => {
    if (openedFromHome.current) window.history.back();
    else window.location.hash = '#/';
  };

  const pool = useMemo(() => new Set(catalog?.tickers.map((t) => t.ticker) ?? []), [catalog]);

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
    return (
      <DocPage
        key={route.ticker}
        ticker={route.ticker}
        row={catalog.tickers.find((t) => t.ticker === route.ticker)}
        pool={pool}
        zoneLabel={(id) => labels.get(id) ?? id}
        onBack={goBack}
      />
    );
  }
  return (
    <>
      {error && <div className="banner-error" onClick={reload}>刷新失败：{error}（点此重试）</div>}
      <HomePage catalog={catalog} filter={filter} onFilter={setFilter} onOpen={openDoc}
        onRefresh={reload} refreshing={loading} />
    </>
  );
}
