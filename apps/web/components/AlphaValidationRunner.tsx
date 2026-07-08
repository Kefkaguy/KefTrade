"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ActionNote, EmptyState, Toast } from "@/components/ResearchUI";
import { runAlphaValidation, type AlphaValidationReport } from "@/lib/api";
import { displayRecommendation } from "@/lib/live-research";
import { money, number, percent } from "@/lib/format";

type RuleDetail = NonNullable<AlphaValidationReport["leaderboard"][number]["evidence_rule_details"]>[string];
type ValidationRow = AlphaValidationReport["leaderboard"][number];

export function AlphaValidationRunner() {
  const router = useRouter();
  const [report, setReport] = useState<AlphaValidationReport | null>(null);
  const [status, setStatus] = useState("Ready");
  const [maxCandidates, setMaxCandidates] = useState(50);
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });

  async function handleRun() {
    setStatus("Running deterministic alpha validation...");
    setToast({ tone: "info", message: "" });
    try {
      const next = await runAlphaValidation(maxCandidates);
      setReport(next);
      setStatus(`Validation run ${next.id} complete: ${next.candidate_count} candidates.`);
      setToast({ tone: "success", message: `Validation run ${next.id} saved. Dashboard and intelligence views will refresh with the new evidence.` });
      router.refresh();
    } catch {
      setStatus("Validation failed.");
      setToast({ tone: "error", message: "Alpha validation failed. Confirm market data, features, and regimes are synced before retrying." });
    }
  }

  const rows = report?.leaderboard.slice(0, 20) ?? [];
  const rejectedRows = rows.filter((row) => row.recommendation === "Reject");
  const focusRow = rejectedRows[0] ?? rows[0];

  return (
    <div className="grid">
      <ActionNote
        title="What this does"
        body="Validates generated alpha candidates across the configured research datasets, applies evidence gates, persists a validation run, and updates live dashboard evidence."
      />
      <div className="toolbar">
        <label className="field">
          <span className="muted">Candidates</span>
          <input type="number" min={1} max={1000} value={maxCandidates} onChange={(event) => setMaxCandidates(Number(event.target.value))} />
        </label>
        <button className="button" onClick={handleRun}>
          Run alpha validation
        </button>
        <span className="muted">{status}</span>
      </div>
      <Toast tone={toast.tone} message={toast.message} />

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
                    <td>{displayRecommendation(row.recommendation)}</td>
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

          {focusRow ? (
            <section className="panel validationExplainPanel">
              <div className="panelHeader">
                <div>
                  <span className="sectionLabel">Validation Diagnostics</span>
                  <h2>Why this failed</h2>
                </div>
                <span className={`status ${focusRow.recommendation === "Validated Alpha" ? "setup" : focusRow.recommendation === "Research More" ? "watchlist" : "avoid"}`}>
                  {focusRow.recommendation}
                </span>
              </div>
              <div className="explainGrid">
                <div className="failureSummary">
                  <strong>{focusRow.candidate_id}</strong>
                  <p>{focusRow.rejection_explanation || fallbackRejectionExplanation(focusRow)}</p>
                  <div className="ruleBadges">
                    {(focusRow.failed_rules ?? []).length ? (
                      focusRow.failed_rules?.map((rule) => (
                        <span className="status avoid" key={rule}>
                          Failed {ruleLabel(rule)}
                        </span>
                      ))
                    ) : (
                      <span className="status setup">No failed evidence rules</span>
                    )}
                    {(focusRow.passed_rules ?? []).map((rule) => (
                      <span className="status setup" key={`passed-${rule}`}>
                        Passed {ruleLabel(rule)}
                      </span>
                    ))}
                  </div>
                </div>
                <div className="takeawayBox">
                  <span className="sectionLabel">Research takeaway</span>
                  <strong>{researchTakeawayTitle(focusRow)}</strong>
                  <p>{researchTakeawayBody(focusRow)}</p>
                </div>
              </div>
              <RuleDetailList row={focusRow} />
              {rejectedRows.length > 1 ? (
                <div className="rejectionList">
                  <h3>Other rejected candidates</h3>
                  {rejectedRows.slice(1).map((row) => (
                    <article key={row.candidate_id}>
                      <strong>{row.candidate_id}</strong>
                      <span>{(row.failed_rules ?? []).map(ruleLabel).join(", ") || "No failed rule details"}</span>
                      <p>{row.rejection_explanation || fallbackRejectionExplanation(row)}</p>
                      <small>{researchTakeawayTitle(row)}: {researchTakeawayBody(row)}</small>
                    </article>
                  ))}
                </div>
              ) : null}
            </section>
          ) : null}

          <section className="panel">
            <h2>Validation report</h2>
            <pre className="reportBlock">{report.markdown_report}</pre>
          </section>
        </>
      ) : (
        <EmptyState title="No validation run yet." body="Run alpha validation to persist evidence and update the live research dashboard." />
      )}
    </div>
  );
}

function RuleDetailList({ row }: { row: ValidationRow }) {
  const details = row.evidence_rule_details ?? {};
  const entries = Object.entries(details);
  if (!entries.length) {
    return (
      <div className="emptyState compact">
        <strong>No rule details returned.</strong>
        <p>The backend did not include per-rule validation diagnostics for this run.</p>
      </div>
    );
  }
  return (
    <div className="ruleDetailGrid">
      {entries.map(([rule, detail]) => (
        <article key={rule} className={detail.passed ? "passed" : "failed"}>
          <div>
            <span className={`status ${detail.passed ? "setup" : "avoid"}`}>{detail.passed ? "Passed" : "Failed"}</span>
            <strong>{ruleLabel(rule)}</strong>
          </div>
          <dl>
            <div>
              <dt>Actual</dt>
              <dd>{formatRuleValue(rule, detail.actual)}</dd>
            </div>
            <div>
              <dt>Threshold</dt>
              <dd>{detail.comparator} {formatRuleValue(rule, detail.threshold)}</dd>
            </div>
          </dl>
          <p>{detail.explanation}</p>
        </article>
      ))}
      <article className={drawdownConcern(row) ? "failed" : "passed"}>
        <div>
          <span className={`status ${drawdownConcern(row) ? "avoid" : "setup"}`}>{drawdownConcern(row) ? "Watch" : "Context"}</span>
          <strong>Drawdown</strong>
        </div>
        <dl>
          <div>
            <dt>Actual</dt>
            <dd>{percent(row.metrics.max_drawdown)}</dd>
          </div>
          <div>
            <dt>Threshold</dt>
            <dd>Not a hard validation gate</dd>
          </div>
        </dl>
        <p>{drawdownConcern(row) ? "Drawdown is large enough to review even though the current backend evidence gates do not reject directly on drawdown." : "Drawdown did not drive this rejection under the current evidence-gate configuration."}</p>
      </article>
    </div>
  );
}

function ruleLabel(rule: string) {
  const labels: Record<string, string> = {
    min_trades: "Minimum trades",
    profit_factor: "Profit factor",
    stability: "Stability score",
    confidence_interval: "Confidence interval",
    confidence_score: "Confidence score"
  };
  return labels[rule] ?? rule.replace(/_/g, " ");
}

function formatRuleValue(rule: string, value: RuleDetail["actual"]) {
  if (value === "infinite") return "Infinite";
  if (value === null || value === undefined || value === "") return "N/A";
  if (rule === "confidence_interval" || rule === "stability") return number(value);
  if (rule === "profit_factor" || rule === "confidence_score") return number(value);
  return String(value);
}

function failed(row: ValidationRow, rule: string) {
  return Boolean(row.evidence_rule_details?.[rule] && !row.evidence_rule_details[rule].passed) || Boolean(row.failed_rules?.includes(rule));
}

function researchTakeawayTitle(row: ValidationRow) {
  if (dataLooksInsufficient(row)) return "Data or features were insufficient";
  if (failed(row, "min_trades")) return "Too few trades";
  if (failed(row, "profit_factor") || Number(row.metrics.total_return ?? 0) < 0 || Number(row.metrics.expectancy_per_trade ?? 0) <= 0) return "Strategy lost money";
  if (failed(row, "stability") || failed(row, "confidence_interval") || failed(row, "confidence_score")) return "Results were unstable";
  return "No hard rejection driver detected";
}

function researchTakeawayBody(row: ValidationRow) {
  if (dataLooksInsufficient(row)) {
    return "The run produced no usable trades. Check that candles, features, regimes, and candidate setup conditions are available before interpreting performance.";
  }
  if (failed(row, "min_trades")) {
    return `The strategy generated ${String(row.metrics.number_of_trades ?? 0)} trades, below the configured minimum. This is a sample-size rejection, not automatically proof that the idea loses money.`;
  }
  if (failed(row, "profit_factor") || Number(row.metrics.total_return ?? 0) < 0 || Number(row.metrics.expectancy_per_trade ?? 0) <= 0) {
    return "The strategy did not produce enough positive expectancy after fees/slippage. Review entry quality, stop placement, and market-regime fit before researching it further.";
  }
  if (failed(row, "stability") || failed(row, "confidence_interval") || failed(row, "confidence_score")) {
    return "The aggregate result may look acceptable, but the evidence is not durable across regimes, assets, or bootstrap samples.";
  }
  return "The backend did not return a clear failed gate for this row. Review the per-rule diagnostics and raw markdown report.";
}

function fallbackRejectionExplanation(row: ValidationRow) {
  const failedRules = row.failed_rules?.map(ruleLabel).join(", ");
  return failedRules ? `Rejected because these evidence rules failed: ${failedRules}.` : "No rejection explanation was returned for this candidate.";
}

function dataLooksInsufficient(row: ValidationRow) {
  const trades = Number(row.metrics.number_of_trades ?? 0);
  const marketResults = Array.isArray(row.market_results) ? row.market_results : [];
  return trades === 0 && (marketResults.length === 0 || marketResults.every((result) => Number((result as { trade_count?: unknown }).trade_count ?? 0) === 0));
}

function drawdownConcern(row: ValidationRow) {
  return Number(row.metrics.max_drawdown ?? 0) >= 0.2;
}
