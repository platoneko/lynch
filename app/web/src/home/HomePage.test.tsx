import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { StrategyPage } from '../strategy/StrategyPage';
import { HomePage } from './HomePage';
import { DEFAULT_FILTER } from './listModel';

const noop = () => {};

describe('rotation strategy documentation', () => {
  it('makes the rules available in a collapsed panel on the home page', () => {
    const html = renderToStaticMarkup(<HomePage
      catalog={{ zones: [], lynch_classes: [], quote_error: null, axes: [], tickers: [] }}
      filter={DEFAULT_FILTER} onFilter={noop} onOpen={noop} onOpenAxis={noop}
      chatCount={0} onOpenChats={noop} earnings={null} onOpenEarnings={noop}
      anomalies={null} onOpenAnomalies={noop} watch={null} onOpenWatch={noop}
      onOpenStrategies={noop} connection={{ offline: true, savedAt: null }}
    />);
    const panel = html.match(/<details[^>]*><summary>创业板 \/ 红利低波 100 调仓规则<\/summary>[\s\S]*?<\/details>/)?.[0];
    expect(panel).toBeDefined();
    expect(panel?.split('>')[0]).not.toMatch(/\bopen\b/);
    expect(panel).toContain('收盘等于或低于当月下沿');
    expect(panel).toContain('8.75%');
  });

  it('shows the same middle-band examples on the strategy page without a recommendation', () => {
    const html = renderToStaticMarkup(
      <StrategyPage snapshot={null} error={false} refreshing={false} onBack={noop} />,
    );
    expect(html).toContain('当月常态仓位是 35%');
    expect(html).toContain('8.75%');
  });
});
