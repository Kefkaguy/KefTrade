import Link from "next/link";
import { notFound } from "next/navigation";
import { Card, DataTable, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getPersistedCandidateProfile, type PersistedCandidateProfile } from "@/lib/api";
import { number, percent } from "@/lib/format";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

export default async function CandidateDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const candidate = await getPersistedCandidateProfile(decodeURIComponent(id)).catch(() => null);
  if (!candidate) notFound();

  return (
    <div className="pageStack">
      <PageTitle
        title={candidate.candidate_id}
        description="Persisted candidate profile built from campaign jobs, elite evidence, deployment lineage, orders, fills, and forward validation state."
        actions={<Link className="button secondary" href="/research">Back to research</Link>}
      />

      <div className="metricGrid">
        <MetricCard label="Lifecycle" value={candidate.state} detail={candidate.deployment_status} />
        <MetricCard label="Research score" value={number(candidate.research_score)} detail={candidate.strategy_family} />
        <MetricCard label="Profit factor" value={number(candidate.profit_factor)} detail="Persisted best or elite metric" tone="warning" />
        <MetricCard label="Forward state" value={String(candidate.forward_performance.state ?? candidate.state)} detail="Simulation only" />
      </div>

      <div className="dashboardGrid">
        <Card title="Identity and Market" eyebrow="Persisted profile">
          <div className="scoreList">
            <span>Campaigns <strong>{candidate.campaign_ids.join(", ") || "None"}</strong></span>
            <span>Assets <strong>{candidate.assets.join(", ")}</strong></span>
            <span>Timeframes <strong>{candidate.timeframes.join(", ")}</strong></span>
            <span>Generation <strong>{candidate.generation_method ?? "Campaign generation"}</strong></span>
            <span>Parent <strong>{candidate.parent_candidate ?? "None"}</strong></span>
          </div>
        </Card>
        <Card title="Research Metrics" eyebrow="Authoritative evidence">
          <div className="scoreList">
            <span>Expectancy <strong>{number(candidate.expectancy)}</strong></span>
            <span>Trades <strong>{String(candidate.trade_count ?? "N/A")}</strong></span>
            <span>Maximum drawdown <strong>{percent(candidate.maximum_drawdown)}</strong></span>
            <span>Stability <strong>{number(candidate.stability)}</strong></span>
          </div>
        </Card>
      </div>

      <Card title="Strategy Definition" eyebrow="Readable frozen definition">
        <DataTable columns={["Field", "Value"]} rows={objectRows(candidate.strategy_definition)} />
      </Card>

      <div className="dashboardGrid">
        <Card title="Validation Gates" eyebrow="Stored pass/fail evidence">
          <DataTable
            columns={["Gate", "Passed", "Failed runs"]}
            rows={candidate.validation_gates.map((row) => [title(row.gate), String(Boolean(row.passed)), String(row.failed_runs ?? 0)])}
          />
        </Card>
        <Card title="Evidence Plan" eyebrow={candidate.evidence_plan.status}>
          <DataTable
            columns={["Missing evidence", "Recommended test", "Falsification"]}
            rows={candidate.evidence_plan.steps.map((row) => [title(row.missing_evidence_reason), String(row.recommended_test ?? ""), String(row.falsification_condition ?? "")])}
          />
        </Card>
      </div>

      <Card title="Cross-Asset and Regime Evidence" eyebrow="Campaign validation runs">
        <DataTable
          columns={["Asset", "Timeframe", "Status", "PF", "Expectancy", "Trades", "Drawdown"]}
          rows={candidate.cross_asset_evidence.map((row) => {
            const metrics = (row.metrics ?? {}) as Record<string, unknown>;
            return [
              String(row.asset ?? ""),
              String(row.timeframe ?? ""),
              String(row.status ?? ""),
              number(metrics.profit_factor),
              number(metrics.expectancy_per_trade),
              String(metrics.number_of_trades ?? 0),
              percent(metrics.max_drawdown),
            ];
          })}
        />
      </Card>

      <div className="dashboardGrid">
        <Card title="Paper Deployment Status" eyebrow="Candidate-linked simulation">
          {candidate.paper_deployment_status.length ? (
            <DataTable
              columns={["Deployment", "Status", "Lifecycle", "Version", "Started"]}
              rows={candidate.paper_deployment_status.map((row) => [
                String(row.deployment_id),
                String(row.status),
                String(row.lifecycle_state),
                String(row.strategy_version),
                formatDate(row.forward_validation_started_at),
              ])}
            />
          ) : <p>No paper deployment is linked to this candidate.</p>}
        </Card>
        <Card title="Forward Performance" eyebrow="Minimum sample required">
          <div className="scoreList">
            <span>Expected trades <strong>{String(candidate.forward_performance.expected_trade_count ?? "N/A")}</strong></span>
            <span>Actual closed trades <strong>{String(candidate.forward_performance.closed_trades ?? 0)}</strong></span>
            <span>Realized PF <strong>{number(candidate.forward_performance.realized_profit_factor)}</strong></span>
            <span>Realized expectancy <strong>{number(candidate.forward_performance.realized_expectancy)}</strong></span>
          </div>
        </Card>
      </div>

      <div className="dashboardGrid wideLeft">
        <Card title="Diagnostic Report" eyebrow="Why this state was assigned">
          <pre className="reportBlock">{candidate.diagnostic_report}</pre>
        </Card>
        <Card title="Readiness Blockers" eyebrow="Deployment guardrails">
          {candidate.readiness_blockers.length ? (
            <div className="scoreList">{candidate.readiness_blockers.map((blocker) => <span key={blocker}>Blocker <strong>{blocker}</strong></span>)}</div>
          ) : <p>No persisted readiness blocker is currently recorded.</p>}
        </Card>
      </div>

      <Card title="Campaign Lineage" eyebrow="Persisted jobs">
        <DataTable
          columns={["Campaign", "Job", "Candidate", "Status", "Completed"]}
          rows={candidate.campaign_lineage.map((row) => [String(row.campaign_id), String(row.job_id), String(row.candidate_id), String(row.status), formatDate(row.completed_at)])}
        />
      </Card>

      <details className="researchDetails">
        <summary>Technical details</summary>
        <pre className="reportBlock">{JSON.stringify(candidate.technical_details, null, 2)}</pre>
      </details>
    </div>
  );
}

function objectRows(value: Record<string, unknown>) {
  return Object.entries(value).map(([key, row]) => [title(key), formatValue(row)]);
}

function formatValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "None";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function title(value: unknown) {
  return String(value ?? "None").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatDate(value: unknown) {
  if (!value) return "N/A";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}
