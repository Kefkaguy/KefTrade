"use client";

import { useState } from "react";
import { runStrategyResearch, type StrategyResearchReport } from "@/lib/api";
import { money, number, percent } from "@/lib/format";

export function StrategyResearchRunner() {
  const [report, setReport] = useState<StrategyResearchReport | null>(null);
  const [status, setStatus] = useState("Ready");

  async function handleRun() {
    setStatus("Running deterministic strategy library comparison...");
    const next = await runStrategyResearch();
    setReport(next);
    setStatus(`Report complete: ${next.run_count} runs ranked.`);
  }

  const topRows = report?.ranking_table.slice(0, 10) ?? [];
  const bestProfitFactor = Math.max(...topRows.map((row) => Number(row.metrics.profit_factor ?? 0)), 1);

  return (
    <div className="grid">
      <div className="toolbar">
        <button className="button" onClick={handleRun}>
          Run strategy research
        </button>
        <span className="muted">{status}</span>
      </div>

      {report ? (
        <>
          <section className="panel">
            <h2>Strategy scorecard</h2>
            <div className="grid">
              {topRows.map((row) => {
                const value = Number(row.metrics.profit_factor ?? 0);
                const width = `${Math.max(4, (value / bestProfitFactor) * 100)}%`;
                return (
                  <div className="barRow" key={row.run_id}>
                    <span>#{row.rank} {row.strategy_name}</span>
                    <div className="barTrack">
                      <div className="barFill" style={{ width }} />
                    </div>
                    <strong>{row.metrics.profit_factor ? number(row.metrics.profit_factor) : "N/A"}</strong>
                  </div>
                );
              })}
            </div>
          </section>

          <div className="tablePanel">
            <table>
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Strategy</th>
                  <th>Recommendation</th>
                  <th>Profit factor</th>
                  <th>Expectancy</th>
                  <th>Max DD</th>
                  <th>Sharpe</th>
                  <th>Win rate</th>
                  <th>Trades</th>
                  <th>Avg win</th>
                  <th>Avg loss</th>
                  <th>Losing streak</th>
                  <th>Avg hold</th>
                </tr>
              </thead>
              <tbody>
                {topRows.map((row) => (
                  <tr key={row.run_id}>
                    <td>{row.rank}</td>
                    <td>{row.strategy_name}_{row.strategy_version}</td>
                    <td>{row.recommendation}</td>
                    <td>{row.metrics.profit_factor ? number(row.metrics.profit_factor) : "N/A"}</td>
                    <td>{money(row.metrics.expectancy_per_trade)}</td>
                    <td>{percent(row.metrics.max_drawdown)}</td>
                    <td>{row.metrics.sharpe_ratio ? number(row.metrics.sharpe_ratio) : "N/A"}</td>
                    <td>{percent(row.metrics.win_rate)}</td>
                    <td>{String(row.metrics.number_of_trades ?? 0)}</td>
                    <td>{money(row.metrics.average_win)}</td>
                    <td>{money(row.metrics.average_loss)}</td>
                    <td>{String(row.metrics.longest_losing_streak ?? 0)}</td>
                    <td>{number(row.metrics.average_holding_time_hours)}h</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <section className="panel">
            <h2>Top report</h2>
            <pre className="reportBlock">{topRows[0]?.markdown_report}</pre>
          </section>

          <div className="grid cols2">
            <section className="panel">
              <h2>Feature correlations</h2>
              <div className="miniTable">
                {(topRows[0]?.feature_correlations ?? []).map((row) => (
                  <div key={row.feature}>
                    <span>{row.feature}</span>
                    <strong>{row.correlation_to_profitable_trade === null ? "N/A" : number(row.correlation_to_profitable_trade)}</strong>
                    <small>{row.sample_size} trades</small>
                  </div>
                ))}
              </div>
            </section>

            <section className="panel">
              <h2>Regime breakdown</h2>
              <div className="miniTable">
                {(topRows[0]?.by_market_regime ?? []).map((row) => (
                  <div key={row.regime}>
                    <span>{row.regime}</span>
                    <strong>{money(row.metrics.expectancy_per_trade)}</strong>
                    <small>{String(row.metrics.number_of_trades ?? 0)} trades</small>
                  </div>
                ))}
              </div>
            </section>
          </div>

          <div className="tablePanel">
            <table>
              <thead>
                <tr>
                  <th>Trade</th>
                  <th>Regime</th>
                  <th>Volatility</th>
                  <th>Outcome</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>PnL</th>
                  <th>Exit reason</th>
                </tr>
              </thead>
              <tbody>
                {(topRows[0]?.trade_explorer ?? []).slice(0, 12).map((trade) => (
                  <tr key={String(trade.trade_number)}>
                    <td>{String(trade.trade_number)}</td>
                    <td>{String(trade.trend_regime)}</td>
                    <td>{String(trade.volatility_regime)}</td>
                    <td>{String(trade.outcome)}</td>
                    <td>{money(trade.entry_price)}</td>
                    <td>{money(trade.exit_price)}</td>
                    <td>{money(trade.pnl)}</td>
                    <td>{String(trade.exit_reason)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </div>
  );
}
