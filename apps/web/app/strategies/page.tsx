import Link from "next/link";
import { Card, DataTable, EmptyState, LineChart, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import { countBy, displayRecommendation, getLiveResearchSnapshot, metricValue, statusClass, timelineItems, validationSeries } from "@/lib/live-research";

export default async function StrategiesPage() {
  const snapshot = await getLiveResearchSnapshot();
  const strategyNames = Array.from(new Set(snapshot.archive.map((row) => row.strategy)));
  const recommendationCounts = countBy(snapshot.archive, (row) => displayRecommendation(row.recommendation));
  const selected = snapshot.archive[0];
  const events = timelineItems(snapshot, 8);
  return (
    <div className="pageStack">
      <PageTitle title="Strategies" description="Deterministic strategy library with evidence rules, validation history, and failure analysis." />
      <div className="metricGrid">
        <MetricCard label="Library size" value={strategyNames.length} detail="Strategies in evidence archive" />
        <MetricCard label="Validated" value={recommendationCounts["Validated Alpha"] ?? 0} detail="Statistical edge proven" tone="success" />
        <MetricCard label="Rejected" value={recommendationCounts.Reject ?? 0} detail="Evidence gates failed" tone="error" />
        <MetricCard label="Needs research" value={recommendationCounts["Research More"] ?? recommendationCounts["Needs More Research"] ?? 0} detail="Insufficient evidence" tone="warning" />
      </div>
      <div className="dashboardGrid wideLeft">
        <Card title="Strategy library" eyebrow="Compare">
          {snapshot.archive.length ? (
            <DataTable
              columns={["Strategy", "Candidate", "Status", "Trades", "Detail"]}
              rows={snapshot.archive.map((row) => [
                <Link className="tableLink" href={`/strategies/${encodeURIComponent(row.strategy)}`} key={row.evidence_ref}>{row.strategy}</Link>,
                row.candidate_id,
                <span className={`status ${statusClass(row.recommendation)}`} key={`${row.evidence_ref}-status`}>{displayRecommendation(row.recommendation)}</span>,
                metricValue(row.metrics, "number_of_trades"),
                row.failure_reasons?.[0] || "No failure reason recorded."
              ])}
            />
          ) : (
            <EmptyState title="No strategy evidence yet." body="Run alpha discovery or validation to populate the strategy library." />
          )}
        </Card>
        <Card title={selected?.strategy ?? "No strategy selected"} eyebrow="Scorecard">
          {selected ? (
            <div className="scoreList">
              <span>Profit Factor <strong>{metricValue(selected.metrics, "profit_factor")}</strong></span>
              <span>Expectancy <strong>{metricValue(selected.metrics, "expectancy_per_trade")}</strong></span>
              <span>Max Drawdown <strong>{metricValue(selected.metrics, "max_drawdown")}</strong></span>
              <span>Trade Count <strong>{metricValue(selected.metrics, "number_of_trades")}</strong></span>
              <span>Recommendation <strong>{displayRecommendation(selected.recommendation)}</strong></span>
            </div>
          ) : (
            <EmptyState title="No scorecard yet." body="Strategy metrics will appear after validation creates evidence rows." />
          )}
        </Card>
      </div>
      <div className="dashboardGrid">
        <Card title="Validation behavior" eyebrow="Risk">
          <LineChart values={validationSeries(snapshot.validationRuns)} label="Strategy validation score history" />
        </Card>
        <Card title="Research timeline" eyebrow="Evidence">
          {events.length ? <Timeline items={events} /> : <EmptyState title="No research timeline yet." body="Run experiments to build strategy history." />}
        </Card>
      </div>
    </div>
  );
}
