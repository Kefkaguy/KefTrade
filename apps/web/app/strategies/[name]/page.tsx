import { notFound } from "next/navigation";
import { BarList, Card, DataTable, LineChart, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import { assets, drawdownCurve, journalEntries, regimeRows, strategies } from "@/lib/research-data";

export default async function StrategyDetailPage({ params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  const strategy = strategies.find((item) => item.name === name);
  if (!strategy) notFound();

  return (
    <div className="pageStack">
      <PageTitle
        title={`${strategy.name}_${strategy.version}`}
        description="Dedicated strategy evidence page showing overview, validation history, assets tested, regimes, evidence rules, and failure analysis."
      />
      <div className="metricGrid">
        <MetricCard label="Recommendation" value={strategy.recommendation} detail="Evidence gate result" tone={strategy.recommendation === "Reject" ? "error" : "warning"} />
        <MetricCard label="Trade count" value={strategy.trades} detail="Below validation confidence" />
        <MetricCard label="Average win" value="Unstable" detail="Varies by year" tone="warning" />
        <MetricCard label="Longest losing streak" value="Material" detail="Needs failure review" tone="error" />
      </div>
      <div className="dashboardGrid">
        <Card title="Validation history" eyebrow="Performance">
          <LineChart values={drawdownCurve} label={`${strategy.name} validation history`} />
        </Card>
        <Card title="Market regimes" eyebrow="Diagnostics">
          <BarList rows={regimeRows} />
        </Card>
      </div>
      <div className="dashboardGrid wideLeft">
        <Card title="Evidence rules" eyebrow="Why it failed">
          <DataTable
            columns={["Rule", "Status", "Interpretation"]}
            rows={[
              ["Profit Factor", <span className="status avoid" key="pf">Failed</span>, "Below validation threshold."],
              ["Stability", <span className="status avoid" key="stability">Failed</span>, "Insufficient durability across years and regimes."],
              ["Confidence Interval", <span className="status avoid" key="ci">Failed</span>, "Too wide for research confidence."],
              ["Trade Count", <span className="status watchlist" key="trades">Watch</span>, `${strategy.trades} trades observed.`]
            ]}
          />
        </Card>
        <Card title="Assets tested" eyebrow="Coverage">
          <div className="assetGrid">
            {assets.map((asset) => <span className="assetChip" key={asset.symbol}>{asset.symbol}</span>)}
          </div>
        </Card>
      </div>
      <Card title="Related hypotheses and timeline" eyebrow="Research">
        <Timeline items={journalEntries} />
      </Card>
    </div>
  );
}
