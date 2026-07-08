import { Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getPaperSnapshot } from "@/lib/paper";
import { money, number } from "@/lib/format";

export default async function PaperPortfolioPage() {
  const snapshot = await getPaperSnapshot();
  return (
    <div className="pageStack">
      <PageTitle title="Paper Portfolio" description="Simulation-only balances, cash, realized PnL, and long positions." />
      {snapshot.account && snapshot.balances ? (
        <>
          <div className="metricGrid">
            <MetricCard label="Equity" value={money(snapshot.balances.equity)} detail="Cash plus simulated market value" />
            <MetricCard label="Cash" value={money(snapshot.balances.cash_balance)} detail="No margin or leverage" />
            <MetricCard label="Market value" value={money(snapshot.balances.market_value)} detail="Long positions only" />
            <MetricCard label="Unrealized PnL" value={money(snapshot.balances.unrealized_pnl)} detail="Marked from candles" />
          </div>
          <Card title="Positions" eyebrow={snapshot.account.name}>
            {snapshot.positions.length ? (
              <DataTable
                columns={["Symbol", "Quantity", "Avg price", "Last", "Market value", "Unrealized", "Realized"]}
                rows={snapshot.positions.map((row) => [
                  row.symbol,
                  number(row.quantity),
                  money(row.average_price),
                  money(row.last_price),
                  money(row.market_value),
                  money(row.unrealized_pnl),
                  money(row.realized_pnl)
                ])}
              />
            ) : (
              <EmptyState title="No simulated positions." body="Filled paper orders will create long-only positions here." />
            )}
          </Card>
        </>
      ) : (
        <EmptyState title="No paper account." body="Create a paper account from the Paper Dashboard." />
      )}
    </div>
  );
}
