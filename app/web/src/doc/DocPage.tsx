import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { AxisLine } from '../home/AxisLine';
import { consensusText, extraBands, rowZone, type ExtraBand } from '../home/listModel';
import { fmt, priceDecimals } from '../home/PriceBar';
import { BandHead } from '../home/TickerCard';
import { docHash } from '../route';
import type { AxisInfo, TickerRow } from '../types';
import { buildDoc, renderFragment, sectionLabel, type BuiltDoc, type LinkTargets } from './buildDoc';
import { currentIndex, isVisible, neighbors, openIdsFor } from './nav';

/** A folding, jumpable markdown document: a ticker's framework or a shared axis. */
export function DocPage({ load: fetchSource, targets, head, onBack }: {
  load: () => Promise<string>; // the page is keyed on its document, so this runs once per mount
  targets: LinkTargets;
  head: ReactNode; // shown above the document's own title block
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
    fetchSource().then(setSource, (e: unknown) => setError(String(e instanceof Error ? e.message : e)));
  };
  // Once per document: the parent keys the page on it.
  useEffect(load, []);

  const doc: BuiltDoc | null = useMemo(() => (source ? buildDoc(source, targets) : null), [source, targets]);

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
  const { up, next } = neighbors(sections.length, current);
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

  return (
    <div className="doc">
      <header className="bar bar-top" ref={topBar}>
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <button className="bar-jump" disabled={up === null} onClick={() => up !== null && jump(up)}>
            ↑ {up !== null ? label(up) : '已到开头'}
          </button>
        </div>
      </header>

      <main className="doc-body">
        <div className="doc-head">
          {head}
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

/** An axis's head: where consensus sits on it, and the tickers mounted on it. */
export function AxisDocHead({ axis, pool }: { axis: AxisInfo | undefined; pool: ReadonlySet<string> }) {
  if (!axis) return null;
  const { consensus } = axis;
  return (
    <div className="axis-head">
      <p><span className="tag tag-axis">{consensusText(consensus)}</span>{consensus?.note && <span className="axis-note">{consensus.note}</span>}</p>
      <p className="axis-mounts">
        挂载票：
        {axis.tickers.map((t) => (pool.has(t) ? <a key={t} href={docHash(t)}>{t}</a> : <span key={t}>{t}</span>))}
      </p>
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
