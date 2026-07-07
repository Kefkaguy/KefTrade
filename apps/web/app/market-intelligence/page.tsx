import { BarList, Card, EmptyState, LineChart, MetricCard, PageTitle } from "@/components/ResearchUI";
import { barRows, countBy, getLiveResearchSnapshot, validationSeries } from "@/lib/live-research";

export default async function MarketIntelligencePage() {
  const snapshot = await getLiveResearchSnapshot();
  const regimeRows = barRows(countBy(snapshot.archive.flatMap((row) => row.market_regimes), (value) => value), "No regimes");
  const assetRows = barRows(countBy(snapshot.archive.flatMap((row) => row.assets), (value) => value), "No assets");
  const regimeMeta = snapshot.intelligence?.meta_analysis.regime_specific_performance ?? [];
  const weakestRegime = regimeMeta.at(-1)?.market_regime as string | undefined;
  const strongestRegime = regimeMeta.at(0)?.market_regime as string | undefined;
  return (
    <div className="pageStack">
      <PageTitle title="Market Intelligence" description="Regime diagnostics explaining where strategies fail, stabilize, or require further evidence." />
      <div className="metricGrid">
        <MetricCard label="Most hostile regime" value={weakestRegime ?? "No data"} detail="Lowest live expectancy group" tone="error" />
        <MetricCard label="Strongest regime" value={strongestRegime ?? "No data"} detail="Highest live expectancy group" tone="success" />
        <MetricCard label="Regime records" value={regimeRows.reduce((sum, row) => sum + row.value, 0)} detail="Archive classifications" tone="warning" />
        <MetricCard label="Assets covered" value={assetRows.filter((row) => row.value > 0).length} detail="Evidence archive assets" />
      </div>
      <div className="dashboardGrid">
        <Card title="Regime distribution" eyebrow="Classification">
          <BarList rows={regimeRows} />
        </Card>
        <Card title="Asset coverage" eyebrow="Coverage">
          <BarList rows={assetRows} />
        </Card>
      </div>
      <div className="dashboardGrid">
        <Card title="Validation history" eyebrow="Risk">
          <LineChart values={validationSeries(snapshot.validationRuns)} label="Validation score history" />
        </Card>
        <Card title="Failure analysis" eyebrow="Diagnostics">
          {snapshot.intelligence?.confidence?.length ? (
            <div className="insightList">
              {snapshot.intelligence.confidence.slice(0, 4).map((item) => (
                <p key={item.conclusion}>{item.conclusion}</p>
              ))}
            </div>
          ) : (
            <EmptyState title="No failure analysis yet." body="Run validation to generate regime and failure diagnostics." />
          )}
        </Card>
      </div>
    </div>
  );
}
