"use client";

import { useState } from "react";
import { runStrategyResearch, type StrategyResearchReport } from "@/lib/api";
import { money, number, percent } from "@/lib/format";

export function StrategyResearchRunner() {
  const [report, setReport] = useState<StrategyResearchReport | null>(null);
  const [status, setStatus] = useState("Ready");

  async function handleRun() {
    setStatus("Running deterministic parameter sweep...");
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
            <h2>Comparison chart</h2>
            <div className="grid">
              {topRows.map((row) => {
                const value = Number(row.metrics.profit_factor ?? 0);
                const width = `${Math.max(4, (value / bestProfitFactor) * 100)}%`;
                return (
                  <div className="barRow" key={row.run_id}>
                    <span>#{row.rank}</span>
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
                  <th>EMA</th>
                  <th>RSI</th>
                  <th>R/R</th>
                  <th>Stop lookback</th>
                  <th>Profit factor</th>
                  <th>Expectancy</th>
                  <th>Max DD</th>
                  <th>Sharpe</th>
                  <th>Win rate</th>
                  <th>Trades</th>
                </tr>
              </thead>
              <tbody>
                {topRows.map((row) => (
                  <tr key={row.run_id}>
                    <td>{row.rank}</td>
                    <td>
                      {String(row.parameters.ema_fast)} / {String(row.parameters.ema_slow)}
                    </td>
                    <td>
                      {String(row.parameters.rsi_min)}-{String(row.parameters.rsi_max)}
                    </td>
                    <td>{String(row.parameters.risk_reward)}</td>
                    <td>{String(row.parameters.swing_lookback)}</td>
                    <td>{row.metrics.profit_factor ? number(row.metrics.profit_factor) : "N/A"}</td>
                    <td>{money(row.metrics.expectancy_per_trade)}</td>
                    <td>{percent(row.metrics.max_drawdown)}</td>
                    <td>{row.metrics.sharpe_ratio ? number(row.metrics.sharpe_ratio) : "N/A"}</td>
                    <td>{percent(row.metrics.win_rate)}</td>
                    <td>{String(row.metrics.number_of_trades ?? 0)}</td>
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
