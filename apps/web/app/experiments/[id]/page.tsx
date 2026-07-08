import Link from "next/link";
import { notFound } from "next/navigation";
import { Card, DataTable, EmptyState, Heatmap, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import { getResearchPortfolio, getStrategyExperiment } from "@/lib/api";
import { getLiveResearchSnapshot, timelineItems } from "@/lib/live-research";
import { number } from "@/lib/format";

export default async function ExperimentDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const experimentId = decodeURIComponent(id);
  const [experiment, portfolio, snapshot] = await Promise.all([
    getStrategyExperiment(experimentId).catch(() => null),
    getResearchPortfolio({ maxCandidates: 80 }).catch(() => null),
    getLiveResearchSnapshot()
  ]);
  if (!experiment) notFound();
  const candidates = portfolio?.candidates.filter((row) => row.experiment_id === experiment.id) ?? [];
  const heatmapRows = candidates.flatMap((candidate) =>
    candidate.dataset_results.map((row) => ({
      x: String(row.symbol ?? "Unknown"),
      y: String(row.timeframe ?? "Unknown"),
      value: Number((row.metrics as Record<string, unknown> | undefined)?.profit_factor ?? 0)
    }))
  );

  return (
    <div className="pageStack">
      <PageTitle
        title={experiment.title}
        description={experiment.hypothesis}
        actions={<Link className="button ghost" href="/experiments">Back to experiments</Link>}
      />
      <div className="metricGrid">
        <MetricCard label="Strategy" value={experiment.strategy} detail="Deterministic strategy family" />
        <MetricCard label="Variables" value={experiment.variables.length} detail="Parameters swept" />
        <MetricCard label="Candidates" value={candidates.length} detail="Portfolio candidates from this experiment" />
        <MetricCard label="Top score" value={number(candidates[0]?.research_score)} detail={candidates[0]?.candidate_id ?? "No candidates"} />
      </div>

      <div className="dashboardGrid">
        <Card title="Cross-asset heatmap" eyebrow="Candidate PF">
          <Heatmap rows={heatmapRows} label={`${experiment.id} cross-asset profit factor`} />
        </Card>
        <Card title="Sweep variables" eyebrow="Parameters">
          <div className="scoreList">
            {Object.entries(experiment.sweep).map(([key, values]) => (
              <span key={key}>{key} <strong>{(values as unknown[]).join(", ")}</strong></span>
            ))}
          </div>
        </Card>
      </div>

      <Card title="Related candidates" eyebrow="Research portfolio">
        {candidates.length ? (
          <DataTable
            columns={["Candidate", "Lifecycle", "Score", "PF", "Trades", "Validation status", "Next experiment"]}
            rows={candidates.map((candidate) => [
              <Link className="tableLink" href={`/candidates/${encodeURIComponent(candidate.candidate_id)}`} key={candidate.candidate_id}>{candidate.candidate_id}</Link>,
              candidate.lifecycle_status,
              number(candidate.research_score),
              number(candidate.aggregate_metrics.profit_factor),
              String(candidate.aggregate_metrics.number_of_trades ?? 0),
              candidate.validation_status,
              candidate.recommended_next_experiment
            ])}
          />
        ) : (
          <EmptyState title="No portfolio candidates yet." body="Run cross-asset research to attach candidate evidence to this experiment." />
        )}
      </Card>

      <div className="dashboardGrid">
        <Card title="Experiment rationale" eyebrow="Research design">
          <p className="muted">{experiment.rationale}</p>
        </Card>
        <Card title="Experiment timeline" eyebrow="Research history">
          <Timeline items={timelineItems(snapshot, 8)} />
        </Card>
      </div>
    </div>
  );
}
