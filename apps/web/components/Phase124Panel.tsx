"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, FlaskConical, ShieldAlert } from "lucide-react";
import { getPhase124Analysis, type Phase124FamilyReport, type Phase124Report } from "@/lib/api";
import { Card, DataTable, EmptyState } from "@/components/ResearchUI";

function num(value: number | null | undefined, digits = 2) {
  if (value == null) return "—";
  if (value === Infinity) return "∞";
  return value.toFixed(digits);
}

function pct(value: number | null | undefined, digits = 1) {
  return value == null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

const DECISION_LABELS: Record<string, string> = {
  retain_for_focused_investigation: "Retain for focused investigation",
  redesign_as_separately_versioned_hypothesis: "Redesign (new hypothesis)",
  gather_more_evidence: "Gather more evidence",
  archive: "Archive"
};

const DECISION_TONE: Record<string, string> = {
  retain_for_focused_investigation: "good",
  redesign_as_separately_versioned_hypothesis: "warn",
  gather_more_evidence: "warn",
  archive: "muted"
};

export function Phase124Panel({ campaignId }: { campaignId: number | null }) {
  const [report, setReport] = useState<Phase124Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedFamily, setExpandedFamily] = useState<string | null>(null);

  useEffect(() => {
    if (!campaignId) return;
    let active = true;
    getPhase124Analysis(campaignId)
      .then((result) => {
        if (active) setReport(result);
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "Could not load the Phase 12.4 analysis.");
      });
    return () => {
      active = false;
    };
  }, [campaignId]);

  if (!campaignId) {
    return (
      <Card title="Phase 12.4: portfolio-wide failure analysis" eyebrow="Trade-level root cause">
        <EmptyState title="No trade-evidence campaign yet" body="Launch a campaign against the Phase 12.3 families to generate trade-level evidence for this analysis." />
      </Card>
    );
  }

  return (
    <>
      <Card title="Phase 12.4: portfolio-wide failure analysis" eyebrow={`Trade-level root cause · Campaign ${campaignId}`}>
        <p className="intradayStrategySummary">
          Explains why each Phase 12.3 family failed, using real per-trade evidence (gross vs. net P&L, exit reasons,
          MFE/MAE, costs, position sizing). No thresholds were changed and no candidate was promoted to reach these
          conclusions — every number below comes from stored trade rows or is explicitly marked as evidence that was
          never persisted.
        </p>
        {error ? <div className="strategyLibraryError" role="alert">{error}</div> : null}
        {!report ? <EmptyState title="Loading Phase 12.4 analysis" body="Reading trade-level evidence." /> : null}

        {report ? (
          <>
            <div className="intradayCandidateWhy" style={{ marginTop: 16 }}>
              <span>Minimum-evidence rules applied to every subgroup</span>
              <ul>
                <li>At least {report.minimum_evidence_rules.min_trades_for_subgroup_evidence} trades</li>
                <li>At least {report.minimum_evidence_rules.min_symbols_for_stability} symbols, {report.minimum_evidence_rules.min_months_for_stability} months</li>
                <li>No single symbol/month above {pct(report.minimum_evidence_rules.max_single_symbol_share_of_net_pnl, 0)} of net P&amp;L</li>
                <li>Net profit factor ≥ {report.minimum_evidence_rules.min_net_profit_factor_for_positive_subgroup}, net expectancy &gt; 0</li>
              </ul>
            </div>

            <div className="intradayPilotNote" style={{ marginTop: 16 }}>
              <ShieldAlert size={14} /> Data-availability gaps (never fabricated): market/volatility regime tags read
              &quot;unknown&quot; for every trade (intraday campaigns don&apos;t compute swing-style regime features at
              15m/30m yet); pre-entry price movement was not persisted; train/validation-split performance is only
              date-bounded, not separately scored.
            </div>

            <div style={{ marginTop: 20 }}>
              <DataTable
                columns={["Family", "Jobs", "Trades", "Gross PF", "Net PF", "Cost impact", "Verdict", "Allocation"]}
                rows={report.families.map((family) => [
                  family.family_name,
                  family.performance_decomposition.total_jobs,
                  family.performance_decomposition.trade_count.toLocaleString(),
                  num(family.performance_decomposition.gross_profit_factor),
                  num(family.performance_decomposition.net_profit_factor),
                  family.performance_decomposition.cost_impact_pct_of_gross_expectancy == null
                    ? "—"
                    : `${family.performance_decomposition.cost_impact_pct_of_gross_expectancy}%`,
                  family.performance_decomposition.verdict.replaceAll("_", " "),
                  <em key="d" className={`familyTag ${DECISION_TONE[family.research_allocation.decision] ?? "muted"}`}>
                    {DECISION_LABELS[family.research_allocation.decision] ?? family.research_allocation.decision}
                  </em>
                ])}
              />
            </div>

            <div className="intradayStrategyGrid" style={{ marginTop: 20 }}>
              {report.families.map((family) => (
                <FamilyFailureCard
                  key={family.architecture}
                  family={family}
                  expanded={expandedFamily === family.architecture}
                  onToggle={() => setExpandedFamily((prev) => (prev === family.architecture ? null : family.architecture))}
                />
              ))}
            </div>
          </>
        ) : null}
      </Card>

      {report ? <AmdInvestigationCard investigation={report.amd_30m_session_momentum_investigation} /> : null}
    </>
  );
}

function FamilyFailureCard({
  family,
  expanded,
  onToggle
}: {
  family: Phase124FamilyReport;
  expanded: boolean;
  onToggle: () => void;
}) {
  const perf = family.performance_decomposition;
  return (
    <article className={`intradayStrategyCard active`}>
      <header>
        <strong>{family.family_name}</strong>
        <em className={`familyTag ${DECISION_TONE[family.research_allocation.decision] ?? "muted"}`}>
          {DECISION_LABELS[family.research_allocation.decision] ?? family.research_allocation.decision}
        </em>
      </header>
      <p className="intradayStrategySummary">
        Gross PF {num(perf.gross_profit_factor)} → Net PF {num(perf.net_profit_factor)} ({perf.verdict.replaceAll("_", " ")}).
        Win rate {pct(perf.win_rate)}, payoff {num(perf.payoff_ratio)}.
      </p>
      <div className="intradayStrategyStats">
        <span>{perf.trade_count.toLocaleString()} trades</span>
        <span>{family.performance_decomposition.total_jobs} jobs</span>
        <span>{family.failure_classifications.length} classification{family.failure_classifications.length === 1 ? "" : "s"}</span>
      </div>
      <button type="button" className="button secondary" style={{ marginTop: 12 }} onClick={onToggle}>
        {expanded ? "Hide detail" : "Show exit / cost / stability detail"}
      </button>
      {expanded ? (
        <div style={{ marginTop: 14 }}>
          <span className="sectionLabel">Failure classifications</span>
          <ul className="intradayRejectionReasons" style={{ marginTop: 6 }}>
            {family.failure_classifications.map((entry) => (
              <li key={entry.classification}>{entry.classification.replaceAll("_", " ")}</li>
            ))}
          </ul>

          <span className="sectionLabel" style={{ marginTop: 14, display: "block" }}>Exit reason breakdown</span>
          <DataTable
            columns={["Exit reason", "% of trades", "Net expectancy", "Win rate", "Avg MFE", "Avg MAE"]}
            rows={family.exit_reason_breakdown.map((row) => [
              row.exit_reason,
              pct(row.pct_of_trades),
              num(row.net_expectancy),
              pct(row.win_rate),
              num(row.average_mfe),
              num(row.average_mae)
            ])}
          />

          <span className="sectionLabel" style={{ marginTop: 14, display: "block" }}>Stability by symbol</span>
          <DataTable
            columns={["Symbol", "Trades", "Net PF", "Net expectancy", "Meets minimum evidence"]}
            rows={family.stability_analysis.by_symbol.map((row) => [
              row.key,
              row.trade_count,
              num(row.net_profit_factor),
              num(row.net_expectancy),
              row.meets_minimum_evidence ? "Yes" : "No"
            ])}
          />

          <div className="intradayCandidateWhy" style={{ marginTop: 14 }}>
            <span>Research allocation</span>
            <ul>
              <li>Strongest subgroup: {family.research_allocation.strongest_subgroup ?? "—"}</li>
              <li>Weakest subgroup: {family.research_allocation.weakest_subgroup ?? "—"}</li>
              <li>Evidence stability: {family.research_allocation.evidence_stability.replaceAll("_", " ")}</li>
              <li>Recommended budget: {family.research_allocation.recommended_research_budget}</li>
              <li>Permitted next action: {family.research_allocation.permitted_next_action}</li>
              <li>Prohibited: {family.research_allocation.prohibited_next_action}</li>
            </ul>
          </div>
        </div>
      ) : null}
    </article>
  );
}

function AmdInvestigationCard({ investigation }: { investigation: Record<string, any> }) {
  if (investigation.insufficient_evidence) {
    return (
      <Card title="AMD 30m long Session Momentum: dedicated investigation" eyebrow="Job-level pass, not an elite promotion">
        <EmptyState title="Insufficient evidence" body={investigation.insufficient_evidence} />
      </Card>
    );
  }

  const gross = investigation.gross_and_net_results;
  const transfers = investigation.transfer_to_other_configurations ?? {};
  const comparisons = investigation.comparison_to_other_symbols ?? {};

  return (
    <Card title="AMD 30m long Session Momentum: dedicated investigation" eyebrow="Job-level pass, not an elite promotion">
      <div className="intradayPilotNote">
        <AlertTriangle size={14} />
        <span>
          These 2 candidates ({(investigation.candidate_ids ?? []).join(", ")}) passed the per-job validation screen
          individually, but produced <strong>zero</strong> elite candidates once campaign-level cross-validation ran.
          They are not promoted, not active, and not recommended for promotion by this analysis.
        </span>
      </div>

      <div className="metricGrid intradayPilotMetrics" style={{ marginTop: 16 }}>
        <div className="metricCard neutral"><span>Trades</span><strong>{gross.trade_count}</strong></div>
        <div className="metricCard neutral"><span>Net profit factor</span><strong>{num(gross.net_profit_factor)}</strong></div>
        <div className="metricCard neutral"><span>Net expectancy</span><strong>{num(gross.net_expectancy)}</strong></div>
        <div className="metricCard warning"><span>Survives best-month removal</span><strong>{investigation.survives_removal_of_best_month ? "Yes" : "No"}</strong></div>
      </div>

      <div className="intradayCandidateWhy" style={{ marginTop: 16 }}>
        <span>Why this transfers nowhere else — the actual reason 2 jobs passed but 0 elites resulted</span>
        <ul>
          <li>15m, same symbol/direction: net PF {num(transfers["15m_same_symbol_direction"]?.net_profit_factor)} — does not transfer</li>
          <li>30m, short direction: net PF {num(transfers["30m_short_direction"]?.net_profit_factor)} — does not transfer</li>
          {Object.entries(comparisons).map(([group, symbols]: [string, any]) => (
            <li key={group}>
              {group.replaceAll("_", " ")}: {Object.entries(symbols as Record<string, any>).map(([symbol, row]) => `${symbol} PF ${num(row.net_profit_factor)}`).join(", ")}
            </li>
          ))}
        </ul>
      </div>
      <p className="intradayStrategyReason" style={{ marginTop: 12 }}>
        The campaign-level elite gate requires the edge to generalize across at least 2 assets; AMD 30m long is the
        only configuration that clears a positive net profit factor anywhere in this comparison, so the candidate&apos;s
        median-across-variants performance fails even though the single AMD job looks strong on its own.
      </p>
    </Card>
  );
}
