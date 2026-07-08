import Link from "next/link";
import { notFound } from "next/navigation";
import { Card, DataTable, DrawdownChart, Heatmap, MetricCard, PageTitle, TradeDistribution } from "@/components/ResearchUI";
import { getResearchPortfolio } from "@/lib/api";
import { number, percent } from "@/lib/format";

export default async function CandidateDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const portfolio = await getResearchPortfolio({ maxCandidates: 80 }).catch(() => null);
  const candidate = portfolio?.candidates.find((row) => row.candidate_id === decodeURIComponent(id));
  if (!candidate) notFound();

  const datasetRows = candidate.dataset_results ?? [];
  const heatmapRows = datasetRows.map((row) => ({
    x: String(row.symbol ?? "Unknown"),
    y: String(row.timeframe ?? "Unknown"),
    value: Number((row.metrics as Record<string, unknown> | undefined)?.profit_factor ?? 0)
  }));
  const drawdowns = datasetRows.map((row) => Number((row.metrics as Record<string, unknown> | undefined)?.max_drawdown ?? 0));
  const expectancy = datasetRows.map((row) => Number((row.metrics as Record<string, unknown> | undefined)?.expectancy_per_trade ?? 0));

  return (
    <div className="pageStack">
      <PageTitle
        title={candidate.candidate_id}
        description="Research candidate drilldown with lifecycle state, cross-asset evidence, out-of-sample behavior, drift, and notebook."
        actions={<Link className="button ghost" href="/portfolio">Back to portfolio</Link>}
      />
      <div className="metricGrid">
        <MetricCard label="Lifecycle" value={candidate.lifecycle_status} detail={candidate.validation_status} />
        <MetricCard label="Research score" value={number(candidate.research_score)} detail="Composite ranking score" />
        <MetricCard label="Profit factor" value={number(candidate.aggregate_metrics.profit_factor)} detail="Aggregate across datasets" tone="warning" />
        <MetricCard label="OOS score" value={number(candidate.out_of_sample_score)} detail="Unseen test windows" />
      </div>

      <div className="dashboardGrid">
        <Card title="Cross-asset heatmap" eyebrow="Profit factor">
          <Heatmap rows={heatmapRows} label={`${candidate.candidate_id} profit factor by asset and timeframe`} />
        </Card>
        <Card title="Drawdown chart" eyebrow="Risk">
          <DrawdownChart values={drawdowns} label={`${candidate.candidate_id} drawdown by dataset`} />
        </Card>
      </div>

      <div className="dashboardGrid">
        <Card title="Expectancy distribution" eyebrow="Dataset results">
          <TradeDistribution values={expectancy} label={`${candidate.candidate_id} expectancy distribution`} />
        </Card>
        <Card title="Evidence drift" eyebrow={candidate.evidence_drift.status}>
          <div className="scoreList">
            <span>Score delta <strong>{number(candidate.evidence_drift.score_delta)}</strong></span>
            <span>OOS delta <strong>{number(candidate.evidence_drift.robustness_delta)}</strong></span>
            <span>Message <strong>{candidate.evidence_drift.message}</strong></span>
          </div>
        </Card>
      </div>

      <Card title="Dataset evidence" eyebrow="Cross-asset results">
        <DataTable
          columns={["Symbol", "Timeframe", "PF", "Expectancy", "Trades", "Drawdown", "Win rate"]}
          rows={datasetRows.map((row) => {
            const metrics = row.metrics as Record<string, unknown>;
            return [
              String(row.symbol),
              String(row.timeframe),
              number(metrics.profit_factor),
              number(metrics.expectancy_per_trade),
              String(metrics.number_of_trades ?? 0),
              percent(metrics.max_drawdown),
              percent(metrics.win_rate)
            ];
          })}
        />
      </Card>

      <div className="dashboardGrid wideLeft">
        <Card title="Research notebook" eyebrow="Generated notes">
          <pre className="reportBlock">{candidate.research_notebook}</pre>
        </Card>
        <Card title="Lifecycle events" eyebrow="Transitions">
          <div className="timeline">
            {candidate.lifecycle_events.map((event) => (
              <article key={event.id ?? `${event.to_state}-${event.created_at}`}>
                <time>{new Date(event.created_at).toLocaleDateString()}</time>
                <div>
                  <h3>{event.from_state || "New"} {"->"} {event.to_state}</h3>
                  <p>{event.reason}</p>
                  <span className="status">{event.to_state}</span>
                </div>
              </article>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
