"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ActionNote, EmptyState, Toast } from "@/components/ResearchUI";
import { runAlphaDiscovery, type AlphaDiscoveryReport } from "@/lib/api";
import { displayRecommendation } from "@/lib/live-research";
import { money, number, percent } from "@/lib/format";

export function AlphaDiscoveryRunner() {
  const router = useRouter();
  const [report, setReport] = useState<AlphaDiscoveryReport | null>(null);
  const [status, setStatus] = useState("Ready");
  const [maxCandidates, setMaxCandidates] = useState(250);
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });

  async function handleRun() {
    setStatus("Running deterministic alpha discovery...");
    setToast({ tone: "info", message: "" });
    try {
      const next = await runAlphaDiscovery(maxCandidates);
      setReport(next);
      setStatus(`Discovery complete: ${next.candidate_count} candidates ranked.`);
      setToast({ tone: "success", message: `Alpha discovery ranked ${next.candidate_count} candidates. Review the leaderboard, then validate promising candidates.` });
      router.refresh();
    } catch {
      setStatus("Discovery failed.");
      setToast({ tone: "error", message: "Alpha discovery failed. Sync market data and features, then try again." });
    }
  }

  const topRows = report?.leaderboard.slice(0, 20) ?? [];

  return (
    <div className="grid">
      <ActionNote
        title="What this does"
        body="Generates deterministic alpha candidates from reusable blocks, backtests them on BTCUSDT 4h, and ranks them. This is research discovery, not trading advice."
      />
      <div className="toolbar">
        <label className="field">
          <span className="muted">Candidates</span>
          <input type="number" min={1} max={5000} value={maxCandidates} onChange={(event) => setMaxCandidates(Number(event.target.value))} />
        </label>
        <button className="button" onClick={handleRun}>
          Run alpha discovery
        </button>
        <span className="muted">{status}</span>
      </div>
      <Toast tone={toast.tone} message={toast.message} />

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
                    <td>{displayRecommendation(row.recommendation)}</td>
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
      ) : (
        <EmptyState title="No discovery run yet." body="Run alpha discovery to generate and rank deterministic research candidates." />
      )}
    </div>
  );
}
