import { notFound } from "next/navigation";
import { BarList, Card, DataTable, LineChart, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import { assets, equityCurve, journalEntries, regimeRows, strategies } from "@/lib/research-data";

export default async function AssetDetailPage({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  const asset = assets.find((item) => item.symbol === symbol.toUpperCase());
  if (!asset) notFound();

  return (
    <div className="pageStack">
      <PageTitle title={asset.symbol} description={`${asset.className} research history, validation summary, strategies tested, and market regimes.`} />
      <div className="metricGrid">
        <MetricCard label="Asset class" value={asset.className} detail={asset.exchange} />
        <MetricCard label="Currency" value={asset.currency} detail="Research denomination" />
        <MetricCard label="Validation" value="Rejected" detail="No validated alpha yet" tone="error" />
        <MetricCard label="Strategies" value={strategies.length} detail="Tested deterministic library" />
      </div>
      <div className="dashboardGrid">
        <Card title="Performance" eyebrow="Summary">
          <LineChart values={equityCurve} label={`${asset.symbol} performance summary`} />
        </Card>
        <Card title="Market regimes" eyebrow="Diagnostics">
          <BarList rows={regimeRows} />
        </Card>
      </div>
      <div className="dashboardGrid wideLeft">
        <Card title="Strategies tested" eyebrow="Validation">
          <DataTable
            columns={["Strategy", "Recommendation", "Failure"]}
            rows={strategies.map((strategy) => [
              `${strategy.name}_${strategy.version}`,
              <span className={`status ${strategy.recommendation === "Reject" ? "avoid" : "watchlist"}`} key={strategy.name}>{strategy.recommendation}</span>,
              strategy.failure
            ])}
          />
        </Card>
        <Card title="Research history" eyebrow="Journal">
          <Timeline items={journalEntries.slice(0, 3)} />
        </Card>
      </div>
    </div>
  );
}
