import { it, expect } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { AxisDocHead } from './DocPage';
import { AxisLine } from '../home/AxisLine';
import type { TickerRow } from '../types';

it('explains that different historical consensus observations belong to their own ticker', () => {
  const axis = { name: 'fake', label: '虚构轴', title: '虚构轴', consensus: null,
    tickers: ['ONE.US', 'TWO.US'], refs: [] };
  const row = (tier: string, date: string) => ({ axes: ['fake'], ref_axes: [], axis_status: 'known', axis_details: [{
    name: 'fake', label: '虚构轴', title: '虚构轴', role: 'priced',
    consensus: { tier, as_of: date, kind: 'historical' },
  }] } as unknown as TickerRow);
  const head = renderToStaticMarkup(<AxisDocHead axis={axis} pool={new Set(axis.tickers)} />);
  const first = renderToStaticMarkup(<AxisLine row={row('M', '2026-01-01')} axes={new Map()} onOpenAxis={() => {}} showDetails />);
  const second = renderToStaticMarkup(<AxisLine row={row('H', '2026-02-01')} axes={new Map()} onOpenAxis={() => {}} showDetails />);
  expect(head).toContain('共识按各票记录查看');
  expect(head).not.toContain('共识未标注');
  expect(first).toContain('2026-01-01');
  expect(second).toContain('2026-02-01');
});
