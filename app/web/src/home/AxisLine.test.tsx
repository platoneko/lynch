import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { AxisLine } from './AxisLine';
import type { TickerRow } from '../types';

describe('saved axis details', () => {
  it('shows this ticker’s dated single-institution reference', () => {
    const row = { axes: ['example'], ref_axes: [], axis_status: 'known', axis_details: [{
      name: 'example', label: '示例', title: '示例轴', role: 'priced',
      consensus: { tier: 'M', as_of: '2026-01-01', kind: 'single_institution', institution: '某机构', source_note: '新闻转述' },
    }] } as unknown as TickerRow;
    const html = renderToStaticMarkup(<AxisLine row={row} axes={new Map()} onOpenAxis={() => {}} />);
    expect(html).toContain('单家参考');
    expect(html).toContain('某机构');
    expect(html).toContain('2026-01-01');
    expect(html).toContain('新闻转述');
  });

  it('explains that missing metadata is unknown', () => {
    const row = { axes: [], ref_axes: [], axis_details: [], axis_status: 'unknown' } as unknown as TickerRow;
    const html = renderToStaticMarkup(<AxisLine row={row} axes={new Map()} onOpenAxis={() => {}} />);
    expect(html).toContain('轴资料待核对');
  });

  it('keeps standalone historical consensus even when axis coverage is incomplete', () => {
    const row = { axes: [], ref_axes: [], axis_details: [], axis_status: 'unknown',
      consensus: { tier: 'M', as_of: '2026-01-01' } } as unknown as TickerRow;
    const html = renderToStaticMarkup(<AxisLine row={row} axes={new Map()} onOpenAxis={() => {}} />);
    expect(html).toContain('本票情景梯');
    expect(html).toContain('轴资料待核对');
  });
});
