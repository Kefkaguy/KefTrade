export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

import { notFound } from "next/navigation";
import { CandleChart } from "@/components/CandleChart";
import { BarList, Card, DataTable, EmptyState, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import { getCandles } from "@/lib/api";
import { money } from "@/lib/format";
import {
  barRows,
  countBy,
  displayAssetClass,
  getLiveResearchSnapshot,
  metricValue,
  statusClass,
  timelineItems
} from "@/lib/live-research";

export default async function AssetDetailPage({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  const snapshot = await getLiveResearchSnapshot();
  const asset = snapshot.symbols.find((item) => item.symbol === symbol.toUpperCase());
  if (!asset) notFound();
  const timeframe = asset.asset_class === "crypto" ? "4h" : "1d";
  const candles = await getCandles(220, { symbol: asset.symbol, timeframe }).catch(() => []);
  const latest = candles.at(-1);
  const assetArchive = snapshot.archive.filter((row) => row.assets.includes(asset.symbol));
  const recommendations = countBy(assetArchive, (row) => row.recommendation);
  const regimes = barRows(countBy(assetArchive.flatMap((row) => row.market_regimes), (value) => value), "No regimes");
  const events = timelineItems(snapshot, 5);

  return (
    <div className="pageStack">
      <PageTitle title={asset.symbol} description={`${displayAssetClass(asset.asset_class)} research history, validation summary, strategies tested, and market regimes.`} />
      <div className="metricGrid">
        <MetricCard label="Asset class" value={displayAssetClass(asset.asset_class)} detail={asset.exchange} />
        <MetricCard label="Latest close" value={latest ? money(latest.close) : "No candles"} detail={`${timeframe} candles`} />
        <MetricCard label="Rejected" value={recommendations.Reject ?? 0} detail="Rejected evidence rows" tone="error" />
        <MetricCard label="Strategies" value={new Set(assetArchive.map((row) => row.strategy)).size} detail="Tested deterministic library" />
      </div>
      <div className="dashboardGrid">
        <Card title="Price history" eyebrow="Market data">
          <CandleChart candles={candles} />
        </Card>
        <Card title="Market regimes" eyebrow="Diagnostics">
          <BarList rows={regimes} />
        </Card>
      </div>
      <div className="dashboardGrid wideLeft">
        <Card title="Strategies tested" eyebrow="Validation">
          {assetArchive.length ? (
            <DataTable
              columns={["Strategy", "Recommendation", "Trades", "Failure"]}
              rows={assetArchive.map((row) => [
                row.strategy,
                <span className={`status ${statusClass(row.recommendation)}`} key={row.evidence_ref}>{row.recommendation}</span>,
                metricValue(row.metrics, "number_of_trades"),
                row.failure_reasons?.[0] || "No failure reason recorded."
              ])}
            />
          ) : (
            <EmptyState title="No strategy evidence for this asset." body="Run validation across this asset to populate strategy coverage." />
          )}
        </Card>
        <Card title="Research history" eyebrow="Journal">
          {events.length ? <Timeline items={events} /> : <EmptyState title="No research history yet." body="Research events will appear here after experiments run." />}
        </Card>
      </div>
    </div>
  );
}
