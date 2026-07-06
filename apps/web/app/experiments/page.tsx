import { Card, DataTable, MetricCard, PageTitle } from "@/components/ResearchUI";
import { strategies } from "@/lib/research-data";

const experimentRows = strategies.map((strategy, index) => ({
  id: `EXP-${String(index + 1).padStart(3, "0")}`,
  hypothesis: index % 2 === 0 ? "ATR expansion improves entries" : "Volume confirmation reduces false breakouts",
  strategy: `${strategy.name}_${strategy.version}`,
  result: strategy.recommendation,
  conclusion: strategy.failure
}));

export default function ExperimentsPage() {
  return (
    <div className="pageStack">
      <PageTitle title="Experiments" description="Reproducible deterministic tests comparing indicators, exits, stops, and holding periods." />
      <div className="metricGrid">
        <MetricCard label="Tracked experiments" value={experimentRows.length} detail="Searchable archive" />
        <MetricCard label="Failure reasons" value="4" detail="Repeated evidence blockers" tone="warning" />
        <MetricCard label="Passed evidence gates" value="0" detail="No forced alpha" tone="success" />
        <MetricCard label="Open next steps" value="3" detail="Hypothesis backlog" />
      </div>
      <Card title="Experiment archive" eyebrow="Research history">
        <DataTable
          columns={["ID", "Hypothesis", "Strategy", "Result", "Conclusion"]}
          rows={experimentRows.map((row) => [
            row.id,
            row.hypothesis,
            row.strategy,
            <span className={`status ${row.result === "Reject" ? "avoid" : "watchlist"}`} key={row.id}>{row.result}</span>,
            row.conclusion
          ])}
        />
      </Card>
    </div>
  );
}
