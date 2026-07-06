"use client";

import { useState } from "react";
import { runAlphaDiscovery, type AlphaDiscoveryReport } from "@/lib/api";
import { money, number, percent } from "@/lib/format";

export function AlphaDiscoveryRunner() {
  const [report, setReport] = useState<AlphaDiscoveryReport | null>(null);
  const [status, setStatus] = useState("Ready");

  async function handleRun() {
    setStatus("Running deterministic alpha discovery...");
    const next = await runAlphaDiscovery(250);
    setReport(next);
    setStatus(`Discovery complete: ${next.candidate_count} candidates ranked.`);
  }

  const topRows = report?.leaderboard.slice(0, 20) ?? [];

  return (
    <div className="grid">
      <div className="toolbar">
        <button className="button" onClick={handleRun}>
          Run alpha discovery
        </button>
        <span className="muted">{status}</span>
      </div>

      {report ? (
        <>
          <section className="panel">
            <h2>Discovery summary</h2>
            <div className="grid cols3">
              <div className="metric">
                <span>Candidates</span>
                <strong>{report.candidate_count}</strong>
              </div>
              <div className="metric">
                <span>Best</span>
                <strong>{String(report.summary.best_candidate ?? "N/A")}</strong>
              </div>
              <div className="metric">
                <span>Best score</span>
                <strong>{number(report.summary.best_alpha_score)}</strong>
              </div>
            </div>
          </section>

          <div className="tablePanel">
            <table>
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Candidate</th>
                  <th>Alpha</th>
                  <th>Confidence</th>
                  <th>Recommendation</th>
                  <th>PF</th>
                  <th>Expectancy</th>
                  <th>Sharpe</th>
                  <th>Sortino</th>
                  <th>Max DD</th>
                  <th>Trades</th>
                  <th>Blocks</th>
                </tr>
              </thead>
              <tbody>
                {topRows.map((row) => (
                  <tr key={row.candidate_id}>
                    <td>{row.rank}</td>
                    <td>{row.candidate_id}</td>
                    <td>{number(row.alpha_score)}</td>
                    <td>{number(row.confidence_score)}</td>
                    <td>{row.recommendation}</td>
                    <td>{row.metrics.profit_factor ? number(row.metrics.profit_factor) : "N/A"}</td>
                    <td>{money(row.metrics.expectancy_per_trade)}</td>
                    <td>{row.metrics.sharpe_ratio ? number(row.metrics.sharpe_ratio) : "N/A"}</td>
                    <td>{row.metrics.sortino_ratio ? number(row.metrics.sortino_ratio) : "N/A"}</td>
                    <td>{percent(row.metrics.max_drawdown)}</td>
                    <td>{String(row.metrics.number_of_trades ?? 0)}</td>
                    <td>{Object.values(row.blocks).join(" / ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <section className="panel">
            <h2>Top alpha report</h2>
            <pre className="reportBlock">{topRows[0]?.alpha_report}</pre>
          </section>
        </>
      ) : null}
    </div>
  );
}
