import { useEffect, useState } from 'react';
import { STAGE_LABELS, executionClose, executionText, percent, reasonText, targetLabels, waitingText,
  type Backtest, type BacktestMetrics, type CurrentBands, type StrategySnapshot, type Target } from './model';

const ASSETS = [
  { key: 'growth', label: '创业板 ETF', ticker: '159915.SZ' },
  { key: 'dividend', label: '红利低波 100 ETF', ticker: '159307.SZ' },
] as const;
const POSITIONS: { key: keyof Target; label: string; ticker?: string }[] = [...ASSETS, { key: 'cash', label: '现金' }];
const METRICS: { label: string; display: (metrics: BacktestMetrics) => string }[] = [
  { label: '投入资金年化', display: (value) => percent(value.annual, 2) },
  { label: '夏普', display: (value) => value.sharpe.toFixed(3) },
  { label: '最大回撤', display: (value) => percent(Math.abs(value.drawdown)) },
];

function BandTable({ bands, label }: { bands: CurrentBands; label: string }) {
  return (
    <>
      <table className="strategy-table">
        <caption>{label}</caption>
        <thead><tr><th scope="col">标的</th><th scope="col">下沿</th><th scope="col">中枢</th><th scope="col">上沿</th></tr></thead>
        <tbody>
          {ASSETS.map(({ key, ticker }) => {
            const band = bands[key];
            return (
              <tr key={key}>
                <th scope="row">{ticker}</th>
                <td>{band?.lower.toFixed(3) ?? '—'}</td>
                <td>{band?.middle.toFixed(3) ?? '—'}</td>
                <td>{band?.upper.toFixed(3) ?? '—'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="strategy-secondary">
        区间拟合依据 {bands.anchor_date}
        {bands.basis_date && <> · 除权口径自 {bands.basis_date} 生效</>}
      </p>
      {bands.dividend?.exit != null && (
        <p className="strategy-secondary">红利低波清仓线约 {bands.dividend.exit.toFixed(4)} 元</p>
      )}
    </>
  );
}

function BacktestTable({ backtest }: { backtest: Backtest }) {
  return (
    <table className="strategy-table">
      <caption>{backtest.label} · {backtest.start} 至 {backtest.end}</caption>
      <thead><tr><th scope="col">指标</th><th scope="col">策略</th><th scope="col">满仓基准</th></tr></thead>
      <tbody>
        {METRICS.map(({ label, display }) => (
          <tr key={label}>
            <th scope="row">{label}</th>
            <td>{display(backtest.candidate)}</td>
            <td>{display(backtest.baseline)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function StrategyPage({ snapshot, error, refreshing, onBack }: {
  snapshot: StrategySnapshot | null;
  error: boolean;
  refreshing: boolean;
  onBack: () => void;
}) {
  const recommendation = snapshot?.recommendation;
  const [, setExpired] = useState(false);
  const executionDate = recommendation?.execution_date;
  useEffect(() => {
    setExpired(false);
    if (!executionDate) return;
    const delay = executionClose(executionDate) - Date.now();
    if (delay <= 0) return;
    const timer = window.setTimeout(() => setExpired(true), delay);
    return () => window.clearTimeout(timer);
  }, [executionDate]);
  const currentBands = snapshot?.current_bands;
  const previousBands = recommendation?.bands;
  const showCurrentBands = currentBands && snapshot.waiting_reason !== 'adjustment' && !error && !refreshing;
  const showPreviousBands = previousBands && (!showCurrentBands
    || previousBands.anchor_date !== currentBands.anchor_date || previousBands.basis_date !== currentBands.basis_date);
  const weights = recommendation && targetLabels(recommendation.target);
  const backtests = snapshot?.backtests_policy === 'dividend_exit_5pct' ? snapshot.backtests : [];
  const waiting = waitingText(snapshot, error) || (refreshing && snapshot ? '正在确认最新策略数据。' : null);
  return (
    <div className="chats">
      <header className="bar bar-top">
        <div className="bar-row">
          <button className="icon-btn" onClick={onBack} aria-label="返回">‹</button>
          <div className="chat-title-main">策略</div>
        </div>
      </header>
      <main className="chats-body strategy-body">
        <div className="strategy-heading">
          <h1>创业板 / 红利低波 100 轮动</h1>
        </div>
        <section className="strategy-panel" aria-labelledby="strategy-recommendation">
          <h2 id="strategy-recommendation">当前推荐仓位</h2>
          {recommendation && weights ? (
            <>
              <dl className="strategy-weights">
                {POSITIONS.map(({ key, label, ticker }) => (
                  <div key={key}>
                    <dt>{label}{ticker && <small>{ticker}</small>}</dt>
                    <dd>{weights[key]}</dd>
                  </div>
                ))}
              </dl>
              <p className="strategy-secondary">按策略资金计算 · 收盘数据 {recommendation.signal_date}</p>
              <p className="strategy-execution">{executionText(recommendation, error || refreshing || snapshot.status !== 'ready')}</p>
              <p>{reasonText(recommendation)}</p>
            </>
          ) : (
            <p>{snapshot || error ? '暂无推荐仓位' : '加载中…'}</p>
          )}
          {waiting && <p className="strategy-notice" role="status">{waiting}</p>}
        </section>
        {(recommendation || currentBands || snapshot?.waiting_reason === 'adjustment') && (
          <details className="strategy-panel">
            <summary>策略状态与区间</summary>
            {recommendation && (
              <>
                <p>最近信号状态 · {STAGE_LABELS[recommendation.stage]}</p>
                {recommendation.dividend_exited && <p>红利低波目标仓位为 0%，收盘跌回上沿以下后恢复配仓。</p>}
                <p>信号时的常态配比 · 创业板 {percent(recommendation.regular_growth)} · 红利低波 {percent(1 - recommendation.regular_growth)}</p>
                <p className="strategy-secondary">
                  {recommendation.signal_date} 收盘（信号当日口径）· 创业板 {recommendation.growth_close.toFixed(3)} · 红利低波 {recommendation.dividend_close.toFixed(3)}
                </p>
              </>
            )}
            {showCurrentBands ? <BandTable bands={currentBands} label="当前区间" /> :
              <p className="strategy-secondary">
                {snapshot?.waiting_reason === 'adjustment' ? '等待除权换算' : '当前区间待确认'}
              </p>}
            {showPreviousBands && <BandTable bands={previousBands} label="上次信号使用的区间" />}
          </details>
        )}
        <details className="strategy-panel">
          <summary>规则与回测</summary>
          <ul className="strategy-rules">
            <li>策略在月末拟合下月区间，按近 63 个交易日波动率的倒数分配常态仓位。每只 ETF 的常态仓位在 20% 至 80% 之间。</li>
            <li>创业板触及上沿时退出。红利低波低于自身上沿时承接资金，否则留现金。创业板回落至中枢后先买回到常态仓位的四分之一，继续回落至中枢与下沿的几何中点后买回到常态仓位的一半，到下沿结束分批买回。</li>
            <li>红利低波收盘达到自身上沿的 105% 时清仓，资金留现金，收盘跌回上沿以下后恢复配仓。两只 ETF 同时处于清仓状态时持有全现金。</li>
            <li>创业板到下沿、下沿以下 10% 和 20% 时，目标分别至少为 45%、55% 和 65%。策略每隔至少五个交易日最多加一档，回到中枢时恢复常态。</li>
            <li>策略用两只 ETF 的前复权日线判断，早期价格由全收益指数回推。除权日开盘前换算区间，收盘后判断，下一交易日收盘执行。</li>
          </ul>
          {backtests.map((backtest) => (
            <BacktestTable key={`${backtest.label}-${backtest.start}-${backtest.end}`} backtest={backtest} />
          ))}
          {!backtests.length && <p className="strategy-secondary">暂无可用回测数据</p>}
          <p className="strategy-secondary">满仓基准按波动配比持有两只 ETF。策略仍待独立样本和实际滑点验证。</p>
        </details>
      </main>
    </div>
  );
}
