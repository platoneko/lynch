import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { AxisLine } from './AxisLine';
import type { TickerRow } from '../types';

describe('home scenario direction', () => {
  const render = (row: Partial<TickerRow>) => renderToStaticMarkup(
    <AxisLine row={row as TickerRow} axes={new Map()} onOpenAxis={() => {}} />);
  const axis = { name: 'aluminium', label: '电解铝价差', title: '电解铝轴', role: 'priced' as const,
    historical_supplement: true, relationship_to_result: 'unverified' as const,
    consensus: { tier: 'L–M', as_of: '2026-10-01', kind: 'historical' as const } };

  it('shows the saved direction and its corresponding values without migration commentary', () => {
    const html = render({ ticker: '000933.SZ', currency: 'CNY', L: 20.49, M: 32.70, H: 33.06,
      axis_status: 'unknown', axis_details: [axis] });
    expect(html).toContain('主推演');
    expect(html).toContain('电解铝价差');
    expect(html).toContain('L–M');
    expect(html).toContain('20.49–32.70');
    for (const detail of ['历史', '待核', '2026-10-01']) expect(html).not.toContain(detail);
  });

  it('keeps a single-institution scenario distinct without expanding its source text', () => {
    const html = render({ ticker: 'EXAMPLE.US', axis_details: [{ ...axis,
      consensus: { tier: 'M', as_of: '2026-01-01', kind: 'single_institution',
        institution: '某机构', source_note: '新闻转述' } }] });
    expect(html).toContain('单家参考 M');
    expect(html).not.toContain('某机构');
    expect(html).not.toContain('新闻转述');
  });

  it('does not replace absent scenario data with a disclaimer or invented direction', () => {
    expect(render({ axis_details: [], axis_status: 'unknown' })).toBe('');
  });

  it('shows a standalone direction even if unrelated axis metadata is incomplete', () => {
    const html = render({ ticker: 'EXAMPLE.US', currency: 'USD', L: 10, M: 20, H: 30,
      axis_details: [], axis_status: 'unknown', consensus: { tier: 'M', as_of: '2026-01-01' } });
    expect(html).toContain('本票主推演');
    expect(html).toContain('20.00');
    expect(html).not.toContain('待核');
  });

  it('does not promote reference-only axes to a company valuation direction', () => {
    const html = render({ ticker: 'EXAMPLE.US', L: 10, M: 20, H: 30,
      axis_details: [{ ...axis, role: 'reference' }] });
    expect(html).toContain('参照');
    expect(html).not.toContain('主推演');
    expect(html).not.toContain('L–M');
  });

  it.each([['<L', '低于 10.00'], ['>H', '高于 30.00']])('shows the saved boundary for %s', (tier, text) => {
    const html = render({ ticker: 'EXAMPLE.US', currency: 'USD', L: 10, M: 20, H: 30,
      axis_details: [{ ...axis, consensus: { ...axis.consensus, tier } }] });
    expect(html).toContain(text);
  });

  it('keeps the institution and relationship note in standalone document details', () => {
    const row = { ticker: 'EXAMPLE.US', axis_details: [], consensus: { tier: 'M', as_of: '2026-01-01',
      kind: 'single_institution', institution: '示例机构', relationship_to_result: 'unverified' } } as unknown as TickerRow;
    const html = renderToStaticMarkup(<AxisLine row={row} axes={new Map()} onOpenAxis={() => {}} showDetails />);
    expect(html).toContain('示例机构');
    expect(html).toContain('旧估值关联待核');
    expect(render(row)).not.toContain('示例机构');
    expect(render(row)).not.toContain('旧估值关联待核');
  });
});
