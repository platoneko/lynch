import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { StrategyPage } from './StrategyPage';
import { executionText, reasonText, targetLabels, waitingText, type Recommendation, type StrategySnapshot } from './model';

const recommendation: Recommendation = {
  signal_date: '2026-11-20',
  execution_date: '2026-11-23',
  execution_passed: false,
  action: 'rebalance',
  reasons: ['low_45'],
  target: { growth: .52, dividend: .48, cash: 0 },
  stage: 'low_45',
  regular_growth: .52,
  growth_close: 2.23,
  dividend_close: 1.067,
};

const snapshot: StrategySnapshot = {
  status: 'ready',
  recommendation,
  current_bands: null,
  waiting_reason: null,
  backtests: [],
};

describe('strategy summary', () => {
  it('explains the actual target even when it exceeds the minimum for this stage', () => {
    expect(reasonText(recommendation)).toBe('创业板收盘触及下沿，目标仓位为 52%。');
  });

  it('shows multiple relevant reasons when an exit leaves money in cash', () => {
    expect(reasonText({ ...recommendation, reasons: ['growth_upper', 'diversification_to_cash'] }))
      .toBe('创业板收盘触及上沿，退出创业板。红利低波暂不加仓，腾出的资金留作现金。');
  });

  it('explains the dividend exit and recovery signals', () => {
    expect(reasonText({ ...recommendation, reasons: ['dividend_overheat_exit'] }))
      .toBe('红利低波收盘达到上沿的 105%，清仓红利低波，资金留现金。');
    expect(reasonText({ ...recommendation, reasons: ['dividend_overheat_reentry'] }))
      .toBe('红利低波收盘跌回上沿以下，恢复按规则配仓。');
  });

  it('reports only growth returning to its usual weight while dividend is out', () => {
    expect(reasonText({ ...recommendation, dividend_exited: true, reasons: ['low_reset'] }))
      .toBe('创业板收盘回到中枢，恢复其常态仓位。');
  });

  it('shows only backtests tagged with the adopted strategy version', () => {
    const metrics = { annual: .12, sharpe: .8, drawdown: -.2 };
    const backtests = [{ label: '样本回测', start: '2024-05-06', end: '2026-11-20', candidate: metrics, baseline: metrics }];
    const page = (value: StrategySnapshot) => renderToStaticMarkup(createElement(StrategyPage, {
      snapshot: value, error: false, refreshing: false, onBack: () => {},
    }));
    expect(page({ ...snapshot, backtests })).not.toContain('样本回测');
    expect(page({ ...snapshot, backtests, backtests_policy: 'dividend_exit_5pct' })).toContain('样本回测');
  });

  it('keeps the displayed allocation at 100% for thirds and fractional targets', () => {
    expect(targetLabels({ growth: 1 / 3, dividend: 1 / 3, cash: 1 / 3 }))
      .toEqual({ growth: '33.4%', dividend: '33.3%', cash: '33.3%' });
    expect(targetLabels({ growth: .0001, dividend: .24004, cash: .75986 }))
      .toEqual({ growth: '0%', dividend: '24%', cash: '76%' });
  });

  it('separates a future action, a historical action and a day without a trigger', () => {
    const before = Date.parse('2026-11-23T14:59:59+08:00');
    const close = Date.parse('2026-11-23T15:00:00+08:00');
    expect(executionText(recommendation, false, before)).toBe('下一交易日收盘执行 · 2026-11-23');
    expect(executionText({ ...recommendation, execution_passed: true }, false, before)).toBe('历史执行日 · 2026-11-23');
    expect(executionText(recommendation, true, before)).toBe('上次建议执行日 · 2026-11-23');
    expect(executionText(recommendation, true, close)).toBe('历史执行日 · 2026-11-23');
    const held: Recommendation = { ...recommendation, action: 'hold', reasons: [] };
    expect(executionText(held, false)).toBe('沿用上次目标，无需调仓');
    expect(reasonText(held)).toBe('本次收盘未触发调仓。');
  });

  it('distinguishes pending adjustment, missing first recommendation, and failed refresh', () => {
    expect(waitingText({ ...snapshot, status: 'waiting', waiting_reason: 'adjustment' }, false))
      .toBe('等待除权换算，当前显示上次已确认的目标。');
    expect(waitingText({ ...snapshot, status: 'waiting', waiting_reason: 'closes', recommendation: null }, false))
      .toBe('等待两只 ETF 的收盘数据。');
    expect(waitingText({ ...snapshot, status: 'empty', recommendation: null }, false)).toBeNull();
    expect(waitingText(snapshot, true)).toBe('策略数据刷新失败，当前显示上次已确认的目标。');
  });
});
