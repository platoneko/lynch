import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { fetchFramework } from '../api';
import { extraBands, rowZone, type ExtraBand } from '../home/listModel';
import { fmt } from '../home/PriceBar';
import { BandHead } from '../home/TickerCard';
import type { TickerRow } from '../types';
import { buildDoc, renderFragment, sectionLabel, type BuiltDoc } from './buildDoc';
import { currentIndex, isVisible, neighbors, openIdsFor } from './nav';

export function DocPage({ ticker, row, pool, zoneLabel, onBack }: {
  ticker: string;
  row: TickerRow | undefined;
  pool: ReadonlySet<string>;
  zoneLabel: (id: string) => string;
  onBack: () => void;
}) {
  const [source, setSource] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const [current, setCurrent] = useState(-1);
  const [pendingJump, setPendingJump] = useState<number | null>(null);
  const topBar = useRef<HTMLElement>(null);

  const load = () => {
    setError(null);
    fetchFramework(ticker).then(setSource, (e: unknown) => setError(String(e instanceof Error ? e.message : e)));
  };
  useEffect(load, [ticker]);

  const doc: BuiltDoc | null = useMemo(() => (source ? buildDoc(source, pool) : null), [source, pool]);

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
  const { prev, next } = neighbors(sections.length, current);
  const label = (i: number) => sectionLabel(sections[i], sections);
  const jump = (i: number) => {
    setOpen((o) => new Set([...o, ...openIdsFor(sections, i)]));
    setPendingJump(i);
  };
  const toggle = (id: string) =>
    setOpen((o) => {
      const n = new Set(o);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  const zone = row ? rowZone(row) : null;

  return (
    <div className="doc">
      <header className="bar bar-top" ref={topBar}>
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <button className="bar-jump" disabled={prev === null} onClick={() => prev !== null && jump(prev)}>
            ↑ {prev !== null ? label(prev) : '已到开头'}
          </button>
        </div>
        <div className="bar-where">当前：{current >= 0 ? label(current) : '题头'}</div>
      </header>

      <main className="doc-body">
        <div className="doc-head">
          {zone && <span className={`tag zone-bg-${zone}`}>{zoneLabel(zone)}</span>}
          {row?.blocked && <span className="tag tag-blocked">BLOCKED</span>}
          {/* A BLOCKED ticker shows no zone at all, +V and candidate included. */}
          {row && !row.blocked && extraBands(row).map((b) => (
            <BandBlock key={b.kind} band={b} pool={pool} zoneLabel={zoneLabel} />
          ))}
          <div className="md" dangerouslySetInnerHTML={{ __html: doc.headerHtml }} />
        </div>
        {sections.filter((s) => isVisible(s, open)).map((s) => {
          const Heading = s.level === 2 ? 'h2' : 'h3';
          const isOpen = open.has(s.id);
          return (
            <section key={s.id} className={`sec sec-${s.level}`}>
              <Heading className="sec-head" data-sec={s.index} onClick={() => toggle(s.id)}>
                <span className="sec-caret">{isOpen ? '▾' : '▸'}</span>
                {s.level === 2 ? sectionLabel(s, sections) : s.title}
              </Heading>
              {isOpen && <div className="md" dangerouslySetInnerHTML={{ __html: s.bodyHtml }} />}
            </section>
          );
        })}
      </main>

      <footer className="bar bar-bottom">
        <button className="bar-jump" disabled={next === null} onClick={() => next !== null && jump(next)}>
          ↓ {next !== null ? label(next) : '已到结尾'}
        </button>
      </footer>
    </div>
  );
}

/**
 * An extra band's head line and its background, collapsed by default:
 * +V — the credited prices, the Outputs V_opt line and the assumption rows it cites;
 * candidate — its prices and the gate assumption's text (why it is only a candidate).
 */
function BandBlock({ band, pool, zoneLabel }: {
  band: ExtraBand;
  pool: ReadonlySet<string>;
  zoneLabel: (id: string) => string;
}) {
  const { L, M, H } = band.anchors;
  // Keyed on the catalog's own band object, which stays the same across scroll re-renders.
  const html = useMemo(() => {
    const sources = band.kind === 'option'
      ? [band.anchors.line, ...band.anchors.rows.map((r) => `**${r.id}** ${r.text}`)]
      : [band.anchors.background];
    return sources.filter((s) => s !== null).map((s) => renderFragment(s, pool));
  }, [band.anchors, pool]);
  return (
    <div className="doc-band">
      <BandHead band={band} zoneLabel={zoneLabel} />
      <details>
        <summary>{band.kind === 'option' ? '期权背景' : '候选背景'}</summary>
        <p className="doc-band-prices">
          {band.kind === 'option'
            ? `+V 修正带 L ${fmt(L)} · M ${fmt(M)} · H ${fmt(H)}`
            : `L′ ${fmt(L)} · M′ ${fmt(M)} · H′ ${fmt(H)}`}
        </p>
        {html.map((h, i) => <div key={i} className="md" dangerouslySetInnerHTML={{ __html: h }} />)}
      </details>
    </div>
  );
}
