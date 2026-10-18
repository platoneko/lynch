import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { Tagged } from '../api';
import { useOnForeground } from '../foreground';
import { useConnection } from '../offline/connection';
import { AxisLine } from '../home/AxisLine';
import { consensusText, extraBands, rowZone, type ExtraBand } from '../home/listModel';
import { fmt, priceDecimals } from '../home/PriceBar';
import { BandHead } from '../home/TickerCard';
import { docHash } from '../route';
import type { AxisInfo, TickerRow } from '../types';
import { buildDoc, renderFragment, sectionLabel, type BuiltDoc, type DocSection, type LinkTargets } from './buildDoc';
import { currentIndex, isVisible, neighbors, openIdsFor } from './nav';
import { countHits, findHits, hitRanges, inertFragment, listMatches, paintHits, revealHit, stepMatch } from './search';

/** A folding, jumpable markdown document: a ticker's framework or a shared axis. */
export function DocPage({ load: fetchSource, targets, head, onBack, barExtra }: {
  load: (held: string | null) => Promise<Tagged<string> | null>; // null: unchanged since the `held` tag
  targets: LinkTargets;
  head: ReactNode; // shown above the document's own title block
  onBack: () => void;
  barExtra?: ReactNode; // right end of the top bar
}) {
  const [source, setSource] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const [current, setCurrent] = useState(-1);
  const [pendingJump, setPendingJump] = useState<number | null>(null);
  const [query, setQuery] = useState<string | null>(null); // null while the search row is closed
  const [cursor, setCursor] = useState<number | null>(null); // the match last jumped to
  const [pendingMatch, setPendingMatch] = useState(false);
  const topBar = useRef<HTMLElement>(null);

  const tag = useRef<string | null>(null);
  const read = (onError: (e: unknown) => void) => {
    fetchSource(tag.current).then((fresh) => {
      if (fresh === null) return;
      tag.current = fresh.etag;
      setSource(fresh.value);
    }, onError);
  };
  const load = () => {
    setError(null);
    read((e) => setError(String(e instanceof Error ? e.message : e)));
  };
  // Once per document: the parent keys the page on it.
  useEffect(load, []);
  // Back in the foreground, a document edited meanwhile is read again (unchanged costs an empty 304). A failure
  // keeps the document on screen: the forward may still be waking up, and the next return asks again.
  useOnForeground(() => read(() => {}));
  // Back online after showing the phone's copy: read again without waiting for a tap or a return to foreground.
  const { offline } = useConnection();
  const wasOffline = useRef(offline);
  useEffect(() => {
    if (wasOffline.current && !offline) read(() => {});
    wasOffline.current = offline;
  }, [offline]);

  const doc: BuiltDoc | null = useMemo(() => (source ? buildDoc(source, targets) : null), [source, targets]);

  // Closed sections are not in the DOM, so search counts their hits in an inert copy of each body.
  const searching = query !== null;
  const bodies = useMemo(() => (doc && searching ? doc.sections.map((s) => inertFragment(s.bodyHtml)) : []), [doc, searching]);
  const matches = useMemo(() => {
    if (!doc || !query) return [];
    return listMatches(doc.sections.map((s, i) =>
      findHits(headingText(s, doc.sections), query).length + countHits(bodies[i], query)));
  }, [doc, bodies, query]);

  // Track which heading the reader is in: the last one scrolled under the top bar.
  useEffect(() => {
    if (!doc) return;
    const onScroll = () => {
      const heads = Array.from(document.querySelectorAll<HTMLElement>('[data-sec]'));
      // A jump parks its heading at scroll-margin-top (--bar-h), which sits below the bar's
      // rendered height; that landing spot must still count as "under the bar".
      const landing = heads.length ? parseFloat(getComputedStyle(heads[0]).scrollMarginTop) : 0;
      const threshold = Math.max(topBar.current?.offsetHeight ?? 0, landing) + 8;
      const tops = heads.map((el) => ({
        index: Number(el.dataset.sec),
        top: el.getBoundingClientRect().top,
      }));
      setCurrent(currentIndex(tops, threshold));
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, [doc, open]);

  // A jump first opens the target (and its h2), then scrolls once it is in the DOM.
  useLayoutEffect(() => {
    if (pendingJump === null) return;
    document.querySelector(`[data-sec="${pendingJump}"]`)?.scrollIntoView({ block: 'start' });
    setPendingJump(null);
  }, [pendingJump, open]);

  // Paint the hits in the open sections, the current one apart; after a jump, bring it into view.
  useLayoutEffect(() => {
    if (!query) return;
    // A section's hits in the order `matches` numbers them: its heading, then its body if open.
    const liveHits = (index: number) => {
      const head = document.querySelector(`[data-sec="${index}"]`);
      const body = head?.nextElementSibling;
      return head ? [...hitRanges(head, query), ...(body ? hitRanges(body, query) : [])] : [];
    };
    const all = [...new Set(matches.map((m) => m.section))].flatMap(liveHits);
    const target = cursor === null ? undefined : liveHits(matches[cursor].section)[matches[cursor].nth];
    if (pendingMatch && target) revealHit(target, topBar.current?.offsetHeight ?? 0);
    setPendingMatch(false);
    return paintHits(all, target);
  }, [query, matches, cursor, pendingMatch, open]);

  if (error) {
    return (
      <div className="status-screen">
        <p>加载失败：{error}</p>
        <button onClick={load}>重试</button>
        <button onClick={onBack}>返回</button>
      </div>
    );
  }
  if (!doc) return <div className="status-screen"><p>加载中…</p></div>;

  const { sections } = doc;
  const { up, next } = neighbors(sections.length, current);
  const label = (i: number) => sectionLabel(sections[i], sections);
  const jump = (i: number) => {
    setOpen((o) => new Set([...o, ...openIdsFor(sections, i)]));
    setPendingJump(i);
  };
  const goMatch = (dir: 1 | -1) => {
    const i = stepMatch(matches, cursor, current, dir);
    if (i === null) return;
    setOpen((o) => new Set([...o, ...openIdsFor(sections, matches[i].section)]));
    setCursor(i);
    setPendingMatch(true);
  };
  const search = (q: string | null) => {
    setQuery(q);
    setCursor(null);
  };
  const toggle = (id: string) =>
    setOpen((o) => {
      const n = new Set(o);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  return (
    <div className="doc">
      <header className="bar bar-top" ref={topBar}>
        {query === null ? (
          <div className="bar-row">
            <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
            <span className="bar-title">{current >= 0 ? label(current) : ''}</span>
            <button className="icon-btn" onClick={() => search('')} aria-label="搜索本文">🔍</button>
            {barExtra}
          </div>
        ) : (
          <div className="bar-row">
            <input className="bar-search" type="text" enterKeyHint="search" autoFocus placeholder="搜索本文"
              value={query} onChange={(e) => search(e.target.value)}
              // Enter while an IME is composing only commits the pinyin; it is not a search.
              onKeyDown={(e) => e.key === 'Enter' && !e.nativeEvent.isComposing && goMatch(e.shiftKey ? -1 : 1)} />
            <span className="bar-count">{cursor === null ? '–' : cursor + 1}/{matches.length}</span>
            <button className="icon-btn" disabled={matches.length === 0} onClick={() => goMatch(-1)} aria-label="上一处">↑</button>
            <button className="icon-btn" disabled={matches.length === 0} onClick={() => goMatch(1)} aria-label="下一处">↓</button>
            <button className="icon-btn" onClick={() => search(null)} aria-label="关闭搜索">✕</button>
          </div>
        )}
      </header>

      <main className="doc-body">
        <div className="doc-head">
          {head}
          <div className="md" dangerouslySetInnerHTML={{ __html: doc.headerHtml }} />
        </div>
        {sections.filter((s) => isVisible(s, open, sections)).map((s) => {
          // A table label folds nothing: an empty anchor marks the jump point and its caption stays in the body.
          if (s.level === 4) {
            return (
              <section key={s.id} className="sec sec-4">
                <div className="sec-anchor" data-sec={s.index} />
                <div className="md" dangerouslySetInnerHTML={{ __html: s.bodyHtml }} />
              </section>
            );
          }
          const Heading = s.level === 2 ? 'h2' : 'h3';
          const isOpen = open.has(s.id);
          return (
            <section key={s.id} className={`sec sec-${s.level}`}>
              <Heading className="sec-head" data-sec={s.index} onClick={() => toggle(s.id)}>
                <span className="sec-caret">{isOpen ? '▾' : '▸'}</span>
                {headingText(s, sections)}
              </Heading>
              {isOpen && <div className="md" dangerouslySetInnerHTML={{ __html: s.bodyHtml }} />}
            </section>
          );
        })}
      </main>

      {/* Both jumps share the bottom bar at equal width; the top bar names the current section, so ↑ needs no name. */}
      <footer className="bar bar-bottom">
        <div className="bar-row">
          <button className="bar-jump" disabled={up === null} onClick={() => up !== null && jump(up)}>
            ↑ {up !== null ? '本节开头' : '已到开头'}
          </button>
          <button className="bar-jump" disabled={next === null} onClick={() => next !== null && jump(next)}>
            ↓ {next !== null ? label(next) : '已到结尾'}
          </button>
        </div>
      </footer>
    </div>
  );
}

/**
 * A heading as shown (and searched): a chapter by its § label, a subsection by its own title;
 * a table label shows no heading, its caption is searched as body.
 */
function headingText(s: DocSection, sections: DocSection[]): string {
  if (s.level === 4) return '';
  return s.level === 2 ? sectionLabel(s, sections) : s.title;
}

/** A framework's head: its zone, extra bands, and the axes it is mounted on. */
export function TickerDocHead({ ticker, row, targets, axes, zoneLabel, onOpenAxis }: {
  ticker: string;
  row: TickerRow | undefined;
  targets: LinkTargets;
  axes: ReadonlyMap<string, AxisInfo>;
  zoneLabel: (id: string) => string;
  onOpenAxis: (name: string) => void;
}) {
  const zone = row ? rowZone(row) : null;
  return (
    <>
      {zone && <span className={`tag zone-bg-${zone}`}>{zoneLabel(zone)}</span>}
      {row && extraBands(row).map((b) => (
        <BandBlock key={b.kind} band={b} targets={targets} zoneLabel={zoneLabel} dp={priceDecimals(ticker)} />
      ))}
      {row && <AxisLine row={row} axes={axes} onOpenAxis={onOpenAxis} />}
    </>
  );
}

/**
 * An axis's head: where consensus sits on it, the tickers whose tiers it sets (定档), and
 * the tickers that only cite or check it (参照) — the consensus speaks for the first group only.
 */
export function AxisDocHead({ axis, pool }: { axis: AxisInfo | undefined; pool: ReadonlySet<string> }) {
  if (!axis) return null;
  const { consensus } = axis;
  const tickerLinks = (tickers: string[]) =>
    tickers.map((t) => (pool.has(t) ? <a key={t} href={docHash(t)}>{t}</a> : <span key={t}>{t}</span>));
  return (
    <div className="axis-head">
      <p><span className="tag tag-axis">{consensusText(consensus)}</span>{consensus?.note && <span className="axis-note">{consensus.note}</span>}</p>
      {axis.tickers.length > 0 && <p className="axis-mounts">定档：{tickerLinks(axis.tickers)}</p>}
      {axis.refs.length > 0 && <p className="axis-mounts">参照：{tickerLinks(axis.refs)}</p>}
    </div>
  );
}

/**
 * An extra band's head line and its background, collapsed by default:
 * +V — the credited prices, the Outputs V_opt line and the assumption rows it cites;
 * candidate — its prices and the gate assumption's text (why it is only a candidate).
 */
function BandBlock({ band, targets, zoneLabel, dp }: {
  band: ExtraBand;
  targets: LinkTargets;
  zoneLabel: (id: string) => string;
  dp: number;
}) {
  const { L, M, H } = band.anchors;
  // Keyed on the catalog's own band object, which stays the same across scroll re-renders.
  const html = useMemo(() => {
    const sources = band.kind === 'option'
      ? [band.anchors.line, ...band.anchors.rows.map((r) => `**${r.id}** ${r.text}`)]
      : [band.anchors.background];
    return sources.filter((s) => s !== null).map((s) => renderFragment(s, targets));
  }, [band.anchors, targets]);
  return (
    <div className="doc-band">
      <BandHead band={band} zoneLabel={zoneLabel} />
      <details>
        <summary>{band.kind === 'option' ? '期权背景' : '候选背景'}</summary>
        <p className="doc-band-prices">
          {band.kind === 'option'
            ? `+V 修正带 L ${fmt(L, dp)} · M ${fmt(M, dp)} · H ${fmt(H, dp)}`
            : `L′ ${fmt(L, dp)} · M′ ${fmt(M, dp)} · H′ ${fmt(H, dp)}`}
        </p>
        {html.map((h, i) => <div key={i} className="md" dangerouslySetInnerHTML={{ __html: h }} />)}
      </details>
    </div>
  );
}
