import { CandidateComparison } from "@/components/CandidateComparison";
import { Card, DataTable, EmptyState, Heatmap, LineChart, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getResearchPortfolio, type LifecycleCandidate, type MetricDefinition } from "@/lib/api";
import { number, percent } from "@/lib/format";
import Link from "next/link";

export default async function ResearchPortfolioPage() {
  const portfolio = await getResearchPortfolio().catch(() => null);
  const candidates = portfolio?.candidates ?? [];
  const best = candidates[0];

  return (
    <div className="pageStack">
      <PageTitle
        title="Research Portfolio"
        description="Lifecycle, evidence drift, candidate comparison, and research notebook coverage for quant research candidates. Research only; validation standards are unchanged."
      />

      {portfolio ? (
        <>
          <div className="metricGrid">
            <MetricCard label="Active" value={String(portfolio.summary.active_candidates ?? 0)} detail="Experimenting, promising, or needs evidence" />
            <MetricCard label="Validation queue" value={String(portfolio.summary.validation_queue ?? 0)} detail="Ready for formal alpha validation" tone="warning" />
            <MetricCard label="Rejected" value={String(portfolio.summary.rejected ?? 0)} detail="Rejected research ideas" tone="error" />
            <MetricCard label="Archived" value={String(portfolio.summary.archived ?? 0)} detail="Closed research threads" />
          </div>

          <Card title="Portfolio candidates" eyebrow="Lifecycle view">
            {candidates.length ? (
              <DataTable
                columns={["Candidate", "Lifecycle", "Validation", "Score", "PF", "Trades", "Drawdown", "Drift", "Evidence"]}
                rows={candidates.map((row) => [
                  <Link className="tableLink" href={`/candidates/${encodeURIComponent(row.candidate_id)}`} key={row.candidate_id}>{row.candidate_id}</Link>,
                  <span className={`status ${statusTone(row.lifecycle_status)}`} key={`${row.candidate_id}-state`}>{row.lifecycle_status}</span>,
                  row.validation_status,
                  metricLabel(row.research_score, portfolio.metric_definitions.research_score),
                  metricLabel(row.aggregate_metrics.profit_factor, portfolio.metric_definitions.profit_factor),
                  metricLabel(row.aggregate_metrics.number_of_trades, portfolio.metric_definitions.trade_count, 0),
                  metricLabel(percent(row.aggregate_metrics.max_drawdown), portfolio.metric_definitions.drawdown, undefined, true),
                  <span className={`status ${row.evidence_drift.status === "Drifting" ? "avoid" : "setup"}`} key={`${row.candidate_id}-drift`}>{row.evidence_drift.status}</span>,
                  row.evidence_summary
                ])}
              />
            ) : (
              <EmptyState title="No research candidates." body="Run cross-asset research to populate the portfolio lifecycle." />
            )}
          </Card>

          <Card title="Candidate comparison" eyebrow="Selectable evidence table">
            <CandidateComparison rows={portfolio.comparison} metrics={portfolio.metric_definitions} />
          </Card>

          <div className="dashboardGrid">
            <Card title="Cross-asset heatmap" eyebrow="Portfolio PF">
              <Heatmap rows={portfolioHeatmap(candidates)} label="Portfolio profit factor by asset and timeframe" />
            </Card>
            <Card title="Research score history" eyebrow="Lifecycle snapshots">
              <LineChart values={scoreHistory(candidates)} label="Research score history" />
            </Card>
          </div>

          <div className="dashboardGrid wideLeft">
            <Card title="Evidence timeline" eyebrow="Chronological decisions">
              <div className="timeline">
                {portfolio.timeline.slice(0, 10).map((event) => (
                  <article key={`${event.candidate_id}-${event.event_type}-${event.timestamp}`}>
                    <time>{formatDate(event.timestamp)}</time>
                    <div>
                      <h3>{event.candidate_id}</h3>
                      <p>{event.summary}</p>
                      <span className="status">{event.event_type.replaceAll("_", " ")}</span>
                    </div>
                  </article>
                ))}
              </div>
            </Card>

            <Card title="Evidence clusters" eyebrow="Strongest pockets">
              <div className="miniTable">
                {portfolio.clusters.length ? (
                  portfolio.clusters.map((cluster) => (
                    <div key={cluster.cluster}>
                      <span>{cluster.cluster}</span>
                      <small>{cluster.top_candidate}</small>
                      <strong>{number(cluster.avg_score)}</strong>
                    </div>
                  ))
                ) : (
                  <EmptyState title="No clusters yet." body="Profitable cross-asset pockets will appear after more experiments." />
                )}
              </div>
            </Card>
          </div>

          {best ? <LifecycleDetail candidate={best} metrics={portfolio.metric_definitions} /> : null}
        </>
      ) : (
        <EmptyState
          title="Research portfolio unavailable."
          body="Start the API and sync research datasets. This page is read-only and does not add execution, brokers, or weaker validation gates."
        />
      )}
    </div>
  );
}

function LifecycleDetail({ candidate, metrics }: { candidate: LifecycleCandidate; metrics: Record<string, MetricDefinition> }) {
  return (
    <div className="dashboardGrid wideLeft">
      <Card title="Evidence drift" eyebrow={candidate.candidate_id}>
        <div className="evidenceSummary">
          <p>{candidate.evidence_drift.message}</p>
          <div>
            <strong>{metricTitle(metrics.research_score)}</strong>
            <p>Score delta: {number(candidate.evidence_drift.score_delta)}</p>
          </div>
          <div>
            <strong>{metricTitle(metrics.out_of_sample_score)}</strong>
            <p>OOS delta: {number(candidate.evidence_drift.robustness_delta)}</p>
          </div>
        </div>
      </Card>
      <Card title="Research notebook" eyebrow="Auto-generated notes">
        <pre className="reportBlock">{candidate.research_notebook}</pre>
      </Card>
    </div>
  );
}

function metricLabel(value: unknown, definition?: MetricDefinition, digits = 2, preformatted = false) {
  const title = definition ? `${definition.measures}\nWhy it matters: ${definition.why_it_matters}\nCalculation: ${definition.calculation}` : undefined;
  return <span className="metricHelp" title={title}>{preformatted ? String(value) : number(value, digits)}</span>;
}

function metricTitle(definition?: MetricDefinition) {
  return definition ? `${definition.label}: ${definition.measures}` : "Metric";
}

function statusTone(status: string) {
  if (status === "Alpha Validation" || status === "Validated" || status === "Promising") return "setup";
  if (status === "Needs More Evidence" || status === "Experimenting") return "watchlist";
  return "avoid";
}

function portfolioHeatmap(candidates: LifecycleCandidate[]) {
  const grouped = new Map<string, { x: string; y: string; values: number[] }>();
  for (const candidate of candidates) {
    for (const result of candidate.dataset_results ?? []) {
      const metrics = result.metrics as Record<string, unknown>;
      const x = String(result.symbol ?? "Unknown");
      const y = String(result.timeframe ?? "Unknown");
      const key = `${x}:${y}`;
      const current = grouped.get(key) ?? { x, y, values: [] };
      current.values.push(Number(metrics.profit_factor ?? 0));
      grouped.set(key, current);
    }
  }
  return Array.from(grouped.values()).map((row) => ({ x: row.x, y: row.y, value: row.values.reduce((sum, value) => sum + value, 0) / row.values.length }));
}

function scoreHistory(candidates: LifecycleCandidate[]) {
  const values = candidates
    .flatMap((candidate) => candidate.lifecycle_events.map((event) => Number(event.metrics?.research_score ?? 0)))
    .filter((value) => Number.isFinite(value) && value > 0);
  return values.length ? values : candidates.map((candidate) => Number(candidate.research_score ?? 0)).filter((value) => value > 0);
}

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
