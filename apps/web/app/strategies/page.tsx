import Link from "next/link";
import { Card, DataTable, LineChart, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import { drawdownCurve, journalEntries, strategies } from "@/lib/research-data";

export default function StrategiesPage() {
  const selected = strategies[0];
  return (
    <div className="pageStack">
      <PageTitle title="Strategies" description="Deterministic strategy library with evidence rules, validation history, and failure analysis." />
      <div className="metricGrid">
        <MetricCard label="Library size" value={strategies.length} detail="Common deterministic interface" />
        <MetricCard label="Validated" value="0" detail="No statistical edge proven" tone="success" />
        <MetricCard label="Rejected" value="5" detail="Evidence gates failed" tone="error" />
        <MetricCard label="Needs research" value="1" detail="Insufficient evidence" tone="warning" />
      </div>
      <div className="dashboardGrid wideLeft">
        <Card title="Strategy library" eyebrow="Compare">
          <DataTable
            columns={["Strategy", "Version", "Status", "Trades", "Detail"]}
            rows={strategies.map((strategy) => [
              <Link className="tableLink" href={`/strategies/${strategy.name}`} key={strategy.name}>{strategy.name}</Link>,
              strategy.version,
              <span className={`status ${strategy.recommendation === "Reject" ? "avoid" : "watchlist"}`} key={`${strategy.name}-status`}>{strategy.recommendation}</span>,
              strategy.trades,
              strategy.failure
            ])}
          />
        </Card>
        <Card title={`${selected.name}_${selected.version}`} eyebrow="Scorecard">
          <div className="scoreList">
            <span>Profit Factor <strong>Below gate</strong></span>
            <span>Expectancy <strong>Unstable</strong></span>
            <span>Max Drawdown <strong>Too high</strong></span>
            <span>Trade Count <strong>{selected.trades}</strong></span>
            <span>Recommendation <strong>{selected.recommendation}</strong></span>
          </div>
        </Card>
      </div>
      <div className="dashboardGrid">
        <Card title="Drawdown behavior" eyebrow="Risk">
          <LineChart values={drawdownCurve} label="Strategy drawdown curve" />
        </Card>
        <Card title="Research timeline" eyebrow="Evidence">
          <Timeline items={journalEntries} />
        </Card>
      </div>
    </div>
  );
}
