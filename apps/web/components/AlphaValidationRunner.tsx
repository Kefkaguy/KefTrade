"use client";

import { useState } from "react";
import { runAlphaValidation, type AlphaValidationReport } from "@/lib/api";
import { money, number, percent } from "@/lib/format";

export function AlphaValidationRunner() {
  const [report, setReport] = useState<AlphaValidationReport | null>(null);
  const [status, setStatus] = useState("Ready");

  async function handleRun() {
    setStatus("Running deterministic alpha validation...");
    const next = await runAlphaValidation(50);
    setReport(next);
    setStatus(`Validation run ${next.id} complete: ${next.candidate_count} candidates.`);
  }

  const rows = report?.leaderboard.slice(0, 20) ?? [];

  return (
    <div className="grid">
      <div className="toolbar">
        <button className="button" onClick={handleRun}>
          Run alpha validation
        </button>
        <span className="muted">{status}</span>
      </div>

      {report ? (
        <>
          <section className="panel">
            <h2>Evidence rules</h2>
            <div className="grid cols3">
              <div className="metric">
                <span>Best</span>
                <strong>{String(report.summary.best_candidate ?? "N/A")}</strong>
              </div>
              <div className="metric">
                <span>Recommendation</span>
                <strong>{String(report.summary.best_recommendation ?? "N/A")}</strong>
              </div>
              <div className="metric">
                <span>Run</span>
                <strong>{report.id}</strong>
              </div>
            </div>
          </section>

          <div className="tablePanel">
            <table>
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Candidate</th>
                  <th>Score</th>
                  <th>Recommendation</th>
                  <th>PF</th>
                  <th>Expectancy</th>
                  <th>Max DD</th>
                  <th>Trades</th>
                  <th>Stability</th>
                  <th>Confidence</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.candidate_id}>
                    <td>{row.rank}</td>
                    <td>{row.candidate_id}</td>
                    <td>{number(row.validation_score)}</td>
                    <td>{row.recommendation}</td>
                    <td>{row.metrics.profit_factor ? number(row.metrics.profit_factor) : "N/A"}</td>
                    <td>{money(row.metrics.expectancy_per_trade)}</td>
                    <td>{percent(row.metrics.max_drawdown)}</td>
                    <td>{String(row.metrics.number_of_trades ?? 0)}</td>
                    <td>{number(row.stability.stability_score)}</td>
                    <td>{number(row.stability.confidence_score)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <section className="panel">
            <h2>Validation report</h2>
            <pre className="reportBlock">{report.markdown_report}</pre>
          </section>
        </>
      ) : null}
    </div>
  );
}
