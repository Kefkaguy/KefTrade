"use client";

import { useState } from "react";
import { runBacktest, type BacktestResult } from "@/lib/api";
import { money, number, percent } from "@/lib/format";
import { Metric } from "./Metric";

export function BacktestRunner() {
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [status, setStatus] = useState("Ready");

  async function handleRun() {
    setStatus("Running walk-forward backtest...");
    const next = await runBacktest();
    setResult(next);
    setStatus(`Backtest #${next.id} complete`);
  }

  const metrics = result?.metrics ?? {};
  const walkForward = metrics.walk_forward as { enabled?: boolean; validation_start?: string; validation_end?: string } | undefined;

  return (
    <div className="grid">
      <div className="toolbar">
        <button className="button" onClick={handleRun}>
          Run trend_pullback_v1
        </button>
        <span className="muted">{status}</span>
      </div>
      {result ? (
        <>
          <div className="grid cols3">
            <Metric label="Total return" value={percent(metrics.total_return)} />
            <Metric label="Profit factor" value={metrics.profit_factor ? number(metrics.profit_factor) : "N/A"} />
            <Metric label="Max drawdown" value={percent(metrics.max_drawdown)} />
            <Metric label="Win rate" value={percent(metrics.win_rate)} />
            <Metric label="Expectancy / trade" value={money(metrics.expectancy_per_trade)} />
            <Metric label="Trades" value={String(metrics.number_of_trades ?? 0)} />
          </div>
          <div className="panel">
            <h2>Walk-forward validation</h2>
            <p className="muted">
              {walkForward?.enabled
                ? `Validation window: ${walkForward.validation_start} to ${walkForward.validation_end}`
                : "Not enough rows for a walk-forward split yet."}
            </p>
          </div>
          <div className="tablePanel">
            <table>
              <thead>
                <tr>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>Reason</th>
                  <th>PNL</th>
                </tr>
              </thead>
              <tbody>
                {result.trades.slice(-12).map((trade, index) => (
                  <tr key={`${String(trade.entry_time)}-${index}`}>
                    <td>{String(trade.entry_time)}</td>
                    <td>{String(trade.exit_time)}</td>
                    <td>{String(trade.exit_reason)}</td>
                    <td>{money(trade.pnl)}</td>
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

