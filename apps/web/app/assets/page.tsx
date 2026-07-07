import { AssetLink, Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { countBy, displayAssetClass, getLiveResearchSnapshot } from "@/lib/live-research";

export default async function AssetsPage() {
  const snapshot = await getLiveResearchSnapshot();
  const byClass = countBy(snapshot.symbols, (asset) => asset.asset_class);
  const activeAssets = snapshot.symbols.filter((asset) => asset.is_active);
  return (
    <div className="pageStack">
      <PageTitle title="Assets" description="Stock-first research coverage with crypto retained as a development data source." />
      <div className="metricGrid">
        <MetricCard label="Assets synced" value={activeAssets.length} detail="Active backend symbols" />
        <MetricCard label="US equities" value={byClass.us_equity ?? 0} detail="Registered equity symbols" />
        <MetricCard label="ETFs" value={byClass.etf ?? 0} detail="Registered ETF symbols" />
        <MetricCard label="Evidence rows" value={snapshot.archive.length} detail="Strategy/archive coverage" />
      </div>
      <Card title="Asset coverage" eyebrow="Universe">
        {snapshot.symbols.length ? (
          <DataTable
            columns={["Symbol", "Asset class", "Exchange", "Currency", "Provider", "Status"]}
            rows={snapshot.symbols.map((asset) => [
              <AssetLink key={asset.symbol} symbol={asset.symbol} />,
              displayAssetClass(asset.asset_class),
              asset.exchange,
              asset.currency,
              asset.primary_provider,
              <span className={asset.is_active ? "status setup" : "status avoid"} key={`${asset.symbol}-status`}>
                {asset.is_active ? "Active" : "Inactive"}
              </span>
            ])}
          />
        ) : (
          <EmptyState title="No assets synced." body="Sync market data to register the first research asset." />
        )}
      </Card>
    </div>
  );
}
