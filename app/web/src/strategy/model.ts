export type Target = { growth: number; dividend: number; cash: number };
export type Stage = 'regular' | 'high_exit' | 'reentry_quarter' | 'reentry_half' | 'low_45' | 'low_55' | 'low_65';
type Band = { lower: number; middle: number; upper: number };

export interface Recommendation {
  signal_date: string;
  execution_date: string;
  execution_passed: boolean;
  action: 'rebalance' | 'hold';
  reasons: string[];
  target: Target;
  stage: Stage;
  regular_growth: number;
  growth_close: number;
  dividend_close: number;
  bands?: CurrentBands;
}

export interface CurrentBands {
  growth: Band | null;
  dividend: Band | null;
  anchor_date: string;
  basis_date: string | null;
}

export interface BacktestMetrics {
  annual: number;
  sharpe: number;
  drawdown: number;
}

export interface Backtest {
  label: string;
  start: string;
  end: string;
  candidate: BacktestMetrics;
  baseline: BacktestMetrics;
}

export interface StrategySnapshot {
  status: 'ready' | 'waiting' | 'empty';
  recommendation: Recommendation | null;
  current_bands: CurrentBands | null;
  waiting_reason: 'closes' | 'adjustment' | 'calendar' | 'history' | null;
  backtests: Backtest[];
}

export const STAGE_LABELS: Record<Stage, string> = {
  regular: '常态',
  high_exit: '高位退出',
  reentry_quarter: '买回常态创业板仓位的四分之一',
  reentry_half: '买回常态创业板仓位的一半',
  low_45: '低位第一档',
  low_55: '低位第二档',
  low_65: '低位第三档',
};

export function percent(fraction: number, decimals = 1): string {
  return `${Number((fraction * 100).toFixed(decimals))}%`;
}

/** Round to tenths of a percentage point, allocating residual tenths by largest remainder. */
export function targetLabels(target: Target): Record<keyof Target, string> {
  const parts = [target.growth, target.dividend, target.cash].map((weight) => {
    const exact = weight * 1000;
    const tenths = Math.floor(exact);
    return { tenths, remainder: exact - tenths };
  });
  const missing = 1000 - parts.reduce((total, part) => total + part.tenths, 0);
  [...parts].sort((a, b) => b.remainder - a.remainder).slice(0, missing)
    .forEach((part) => { part.tenths += 1; });
  const [growth, dividend, cash] = parts.map((part) => `${part.tenths / 10}%`);
  return { growth, dividend, cash };
}

export function reasonText(recommendation: Recommendation): string {
  if (recommendation.action === 'hold') return '本次收盘未触发调仓。';
  const growth = targetLabels(recommendation.target).growth;
  const reasons: Readonly<Record<string, string>> = {
    month_start: '月初按最新常态配比和当前策略状态调整仓位。',
    growth_upper: '创业板收盘触及上沿，退出创业板。',
    diversification_to_cash: '红利低波暂不加仓，腾出的资金留作现金。',
    diversification_to_dividend: '红利低波低于上沿，腾出的资金转入红利低波。',
    reentry_quarter: `创业板收盘跌至中枢，买回常态仓位的四分之一，目标仓位为 ${growth}。`,
    reentry_half: `创业板收盘跌至中枢和下沿的几何中点，买回至常态仓位的一半，目标仓位为 ${growth}。`,
    reentry_complete: '创业板收盘跌至下沿，结束分批买回阶段。',
    low_45: `创业板收盘触及下沿，目标仓位为 ${growth}。`,
    low_55: `创业板进入低位第二档，目标仓位为 ${growth}。`,
    low_65: `创业板进入低位第三档，目标仓位为 ${growth}。`,
    low_reset: '创业板收盘回到中枢，恢复常态配比。',
  };
  return recommendation.reasons.map((reason) => reasons[reason]).filter(Boolean).join('') || '策略触发调仓。';
}

export function executionClose(day: string): number {
  return Date.parse(`${day}T15:00:00+08:00`);
}

export function executionText(recommendation: Recommendation, waiting: boolean, now = Date.now()): string {
  if (recommendation.action === 'hold') return waiting ? '上次建议维持原目标' : '沿用上次目标，无需调仓';
  const date = recommendation.execution_date;
  if (recommendation.execution_passed || now >= executionClose(date)) return `历史执行日 · ${date}`;
  if (waiting) return `上次建议执行日 · ${date}`;
  return `下一交易日收盘执行 · ${date}`;
}

export function waitingText(snapshot: StrategySnapshot | null, failed: boolean): string | null {
  const suffix = snapshot?.recommendation ? '，当前显示上次已确认的目标。' : '。';
  if (failed) return `策略数据刷新失败${suffix}`;
  switch (snapshot?.waiting_reason) {
    case 'adjustment': return `等待除权换算${suffix}`;
    case 'calendar': return `等待确认交易日历${suffix}`;
    case 'history': return `等待历史数据补齐${suffix}`;
    case 'closes': return `等待两只 ETF 的收盘数据${suffix}`;
    default: return snapshot?.status === 'waiting' ? `等待策略数据更新${suffix}` : null;
  }
}
