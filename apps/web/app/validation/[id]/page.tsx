import { notFound } from "next/navigation";
import { Card, DataTable, DrawdownChart, EmptyState, Heatmap, MetricCard, PageTitle, TradeDistribution } from "@/components/ResearchUI";
import { getValidationRun } from "@/lib/api";
import { displayRecommendation, statusClass } from "@/lib/live-research";
import { money, number, percent } from "@/lib/format";

export default async function ValidationRunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const run = await getValidationRun(id).catch(() => null);
  if (!run) notFound();
  const rows = run.report?.leaderboard ?? [];
  const best = rows[0];
  const heatmapRows = rows.flatMap((row) =>
    (row.market_results ?? []).map((result) => ({
      x: String(result.symbol ?? "Unknown"),
      y: String(result.timeframe ?? "Unknown"),
      value: Number((result.metrics as Record<string, unknown> | undefined)?.profit_factor ?? result.profit_factor ?? 0)
    }))
  );
  const expectancy = rows.map((row) => Number(row.metrics.expectancy_per_trade ?? 0));
  const drawdowns = rows.map((row) => Number(row.metrics.max_drawdown ?? 0));

  return (
    <div className="pageStack">
      <PageTitle title={`Validation Run ${run.id}`} description="Saved alpha validation evidence, thresholds, failed rules, cross-asset behavior, and generated report." />
      <div className="metricGrid">
        <MetricCard label="Candidates" value={run.candidate_count} detail="Configured candidate count" />
        <MetricCard label="Best score" value={number(best?.validation_score)} detail={best?.candidate_id ?? "No candidates"} />
        <MetricCard label="Best PF" value={number(best?.metrics.profit_factor)} detail="Top-ranked candidate" tone="warning" />
        <MetricCard label="Recommendation" value={displayRecommendation(String(best?.recommendation ?? "N/A"))} detail="Top row only" />
      </div>

      <div className="dashboardGrid">
        <Card title="Cross-asset heatmap" eyebrow="Profit factor">
          <Heatmap rows={heatmapRows} label={`Validation run ${run.id} cross-asset profit factor`} />
        </Card>
        <Card title="Drawdown chart" eyebrow="Risk">
          <DrawdownChart values={drawdowns} label={`Validation run ${run.id} drawdown by candidate`} />
        </Card>
      </div>

      <div className="dashboardGrid">
        <Card title="Expectancy distribution" eyebrow="Candidate PnL">
          <TradeDistribution values={expectancy} label={`Validation run ${run.id} expectancy distribution`} />
        </Card>
        <Card title="Thresholds" eyebrow="Evidence gates">
          <div className="scoreList">
            {Object.entries(run.thresholds ?? {}).map(([key, value]) => (
              <span key={key}>{key.replaceAll("_", " ")} <strong>{String(value)}</strong></span>
            ))}
          </div>
        </Card>
      </div>

      <Card title="Candidate leaderboard" eyebrow="Validation results">
        {rows.length ? (
          <DataTable
            columns={["Candidate", "Recommendation", "PF", "Expectancy", "Trades", "Drawdown", "Failed rules"]}
            rows={rows.slice(0, 30).map((row) => [
              row.candidate_id,
              <span className={`status ${statusClass(row.recommendation)}`} key={row.candidate_id}>{displayRecommendation(row.recommendation)}</span>,
              number(row.metrics.profit_factor),
              money(row.metrics.expectancy_per_trade),
              String(row.metrics.number_of_trades ?? 0),
              percent(row.metrics.max_drawdown),
              (row.failed_rules ?? []).join(", ") || "None"
            ])}
          />
        ) : (
          <EmptyState title="No candidate rows." body="This validation run did not store a leaderboard." />
        )}
      </Card>

      <Card title="Validation report" eyebrow="Markdown">
        <pre className="reportBlock">{run.markdown_report}</pre>
      </Card>
    </div>
  );
}
