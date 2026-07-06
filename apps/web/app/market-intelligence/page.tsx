import { BarList, Card, LineChart, MetricCard, PageTitle } from "@/components/ResearchUI";
import { drawdownCurve, equityCurve, regimeRows } from "@/lib/research-data";

export default function MarketIntelligencePage() {
  return (
    <div className="pageStack">
      <PageTitle title="Market Intelligence" description="Regime diagnostics explaining where strategies fail, stabilize, or require further evidence." />
      <div className="metricGrid">
        <MetricCard label="Most hostile regime" value="Sideways" detail="Negative expectancy repeats" tone="error" />
        <MetricCard label="Strongest regime" value="Bull trend" detail="Least hostile failure profile" tone="success" />
        <MetricCard label="Volatility signal" value="Mixed" detail="Compression needs testing" tone="warning" />
        <MetricCard label="Trend strength" value="Unstable" detail="Requires cross-asset proof" />
      </div>
      <div className="dashboardGrid">
        <Card title="Regime heatmap" eyebrow="Classification">
          <BarList rows={regimeRows} />
        </Card>
        <Card title="Research evolution" eyebrow="Timeline">
          <LineChart values={equityCurve} label="Research evolution" />
        </Card>
      </div>
      <div className="dashboardGrid">
        <Card title="Drawdown curve" eyebrow="Risk">
          <LineChart values={drawdownCurve} label="Drawdown curve" />
        </Card>
        <Card title="Failure analysis" eyebrow="Diagnostics">
          <div className="insightList">
            <p>Sideways markets repeatedly degrade expectancy and produce unstable exits.</p>
            <p>Low volatility regimes show insufficient follow-through for breakout variants.</p>
            <p>High volatility improves opportunity but widens confidence intervals.</p>
          </div>
        </Card>
      </div>
    </div>
  );
}
