import { notFound } from "next/navigation";
import { BarList, Card, DataTable, EmptyState, LineChart, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import {
  barRows,
  countBy,
  getLiveResearchSnapshot,
  metricValue,
  recommendationTone,
  statusClass,
  timelineItems,
  validationSeries
} from "@/lib/live-research";

export default async function StrategyDetailPage({ params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  const snapshot = await getLiveResearchSnapshot();
  const decodedName = decodeURIComponent(name);
  const strategyRows = snapshot.archive.filter((item) => item.strategy === decodedName || item.candidate_id === decodedName);
  const strategy = strategyRows[0];
  if (!strategy) notFound();
  const regimes = barRows(countBy(strategyRows.flatMap((row) => row.market_regimes), (value) => value), "No regimes");
  const assets = Array.from(new Set(strategyRows.flatMap((row) => row.assets)));
  const events = timelineItems(snapshot, 8);

  return (
    <div className="pageStack">
      <PageTitle
        title={strategy.strategy}
        description="Dedicated strategy evidence page showing overview, validation history, assets tested, regimes, evidence rules, and failure analysis."
      />
      <div className="metricGrid">
        <MetricCard label="Recommendation" value={strategy.recommendation} detail="Evidence gate result" tone={recommendationTone(strategy.recommendation)} />
        <MetricCard label="Trade count" value={metricValue(strategy.metrics, "number_of_trades")} detail="Validation sample size" />
        <MetricCard label="Profit factor" value={metricValue(strategy.metrics, "profit_factor")} detail="Evidence metric" tone="warning" />
        <MetricCard label="Max drawdown" value={metricValue(strategy.metrics, "max_drawdown")} detail="Risk metric" tone="error" />
      </div>
      <div className="dashboardGrid">
        <Card title="Validation history" eyebrow="Performance">
          <LineChart values={validationSeries(snapshot.validationRuns)} label={`${strategy.strategy} validation history`} />
        </Card>
        <Card title="Market regimes" eyebrow="Diagnostics">
          <BarList rows={regimes} />
        </Card>
      </div>
      <div className="dashboardGrid wideLeft">
        <Card title="Evidence rules" eyebrow="Why it failed">
          <DataTable
            columns={["Rule", "Status", "Interpretation"]}
            rows={[
              ["Profit Factor", metricValue(strategy.metrics, "profit_factor"), "Current recorded profit factor."],
              ["Expectancy", metricValue(strategy.metrics, "expectancy_per_trade"), "Average expected result per trade."],
              ["Sharpe", metricValue(strategy.metrics, "sharpe_ratio"), "Risk-adjusted return metric."],
              ["Recommendation", <span className={`status ${statusClass(strategy.recommendation)}`} key="recommendation">{strategy.recommendation}</span>, strategy.failure_reasons?.[0] || "No failure reason recorded."]
            ]}
          />
        </Card>
        <Card title="Assets tested" eyebrow="Coverage">
          {assets.length ? (
            <div className="assetGrid">
              {assets.map((asset) => <span className="assetChip" key={asset}>{asset}</span>)}
            </div>
          ) : (
            <EmptyState title="No asset coverage yet." body="Validation output did not record asset-level coverage for this strategy." />
          )}
        </Card>
      </div>
      <Card title="Related hypotheses and timeline" eyebrow="Research">
        {events.length ? <Timeline items={events} /> : <EmptyState title="No timeline yet." body="Related research events will appear after experiments run." />}
      </Card>
    </div>
  );
}
