import Link from "next/link";
import { Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getStrategyExperiments } from "@/lib/api";
import { countBy, getLiveResearchSnapshot, latestExperimentRows, statusClass } from "@/lib/live-research";

export default async function ExperimentsPage() {
  const [snapshot, definitions] = await Promise.all([getLiveResearchSnapshot(), getStrategyExperiments().catch(() => [])]);
  const recommendationCounts = countBy(snapshot.archive, (row) => row.recommendation);
  const failureReasons = new Set(snapshot.archive.flatMap((row) => row.failure_reasons || []));
  const experimentRows = latestExperimentRows(snapshot.archive, 20);
  return (
    <div className="pageStack">
      <PageTitle title="Experiments" description="Reproducible deterministic tests comparing indicators, exits, stops, and holding periods." />
      <div className="metricGrid">
        <MetricCard label="Experiment definitions" value={definitions.length} detail="Deterministic sweeps available" />
        <MetricCard label="Failure reasons" value={failureReasons.size} detail="Repeated evidence blockers" tone="warning" />
        <MetricCard label="Passed evidence gates" value={recommendationCounts["Validated Alpha"] ?? 0} detail="Validated alpha records" tone="success" />
        <MetricCard label="Open hypotheses" value={snapshot.hypotheses.length} detail="Research backlog" />
      </div>
      <Card title="Run research workflow" eyebrow="Next actions">
        <div className="dashboardGrid">
          <div className="actionNote">
            <strong>Hypothesis experiment</strong>
            <p>Create or choose a hypothesis, then run deterministic generated candidates against validation datasets.</p>
            <Link className="button compact" href="/hypotheses">Open hypotheses</Link>
          </div>
          <div className="actionNote">
            <strong>Alpha discovery and validation</strong>
            <p>Generate candidates in discovery, then validate candidates through evidence gates before asking the copilot about results.</p>
            <div className="toolbar">
              <Link className="button compact" href="/alpha">Discovery</Link>
              <Link className="button compact secondary" href="/validation">Validation</Link>
            </div>
          </div>
        </div>
      </Card>
      <Card title="Experiment definitions" eyebrow="Deterministic sweeps">
        {definitions.length ? (
          <DataTable
            columns={["Experiment", "Strategy", "Variables", "Rationale"]}
            rows={definitions.map((experiment) => [
              <Link className="tableLink" href={`/experiments/${encodeURIComponent(experiment.id)}`} key={experiment.id}>{experiment.title}</Link>,
              experiment.strategy,
              experiment.variables.join(", "),
              experiment.rationale
            ])}
          />
        ) : (
          <EmptyState title="No experiment definitions available." body="The API did not return strategy experiment metadata." />
        )}
      </Card>
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
          <EmptyState
            title="No experiments yet."
            body="Run alpha discovery or validation to create the first experiment record."
            action={<Link className="button" href="/alpha">Run alpha discovery</Link>}
          />
        )}
      </Card>
    </div>
  );
}
