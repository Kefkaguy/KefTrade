import { AssetLink, Card, DataTable, MetricCard, PageTitle } from "@/components/ResearchUI";
import { assets, strategies } from "@/lib/research-data";

export default function AssetsPage() {
  return (
    <div className="pageStack">
      <PageTitle title="Assets" description="Stock-first research coverage with crypto retained as a development data source." />
      <div className="metricGrid">
        <MetricCard label="Assets synced" value={assets.length} detail="Crypto and US equities" />
        <MetricCard label="US equities" value="6" detail="SPY, QQQ, AAPL, MSFT, NVDA, TSLA" />
        <MetricCard label="Timeframe" value="1D" detail="Equity validation baseline" />
        <MetricCard label="Strategies tested" value={strategies.length} detail="Shared deterministic library" />
      </div>
      <Card title="Asset coverage" eyebrow="Universe">
        <DataTable
          columns={["Symbol", "Asset class", "Exchange", "Currency", "Status"]}
          rows={assets.map((asset) => [
            <AssetLink key={asset.symbol} symbol={asset.symbol} />,
            asset.className,
            asset.exchange,
            asset.currency,
            <span className="status setup" key={`${asset.symbol}-status`}>{asset.status}</span>
          ])}
        />
      </Card>
    </div>
  );
}
