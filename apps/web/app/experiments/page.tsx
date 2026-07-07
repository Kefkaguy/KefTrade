import { Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { countBy, getLiveResearchSnapshot, latestExperimentRows, statusClass } from "@/lib/live-research";

export default async function ExperimentsPage() {
  const snapshot = await getLiveResearchSnapshot();
  const recommendationCounts = countBy(snapshot.archive, (row) => row.recommendation);
  const failureReasons = new Set(snapshot.archive.flatMap((row) => row.failure_reasons || []));
  const experimentRows = latestExperimentRows(snapshot.archive, 20);
  return (
    <div className="pageStack">
      <PageTitle title="Experiments" description="Reproducible deterministic tests comparing indicators, exits, stops, and holding periods." />
      <div className="metricGrid">
        <MetricCard label="Tracked experiments" value={snapshot.archive.length} detail="Searchable evidence archive" />
        <MetricCard label="Failure reasons" value={failureReasons.size} detail="Repeated evidence blockers" tone="warning" />
        <MetricCard label="Passed evidence gates" value={recommendationCounts["Validated Alpha"] ?? 0} detail="Validated alpha records" tone="success" />
        <MetricCard label="Open hypotheses" value={snapshot.hypotheses.length} detail="Research backlog" />
      </div>
      <Card title="Experiment archive" eyebrow="Research history">
        {experimentRows.length ? (
          <DataTable
            columns={["Candidate", "Strategy", "Result", "Trades", "Conclusion"]}
            rows={experimentRows.map((row) => [
              row.candidate,
              row.strategy,
              <span className={`status ${statusClass(row.recommendation)}`} key={row.candidate}>{row.recommendation}</span>,
              row.trades,
              row.failure
            ])}
          />
        ) : (
          <EmptyState title="No experiments yet." body="Run alpha discovery or validation to create the first experiment record." />
        )}
      </Card>
    </div>
  );
}
