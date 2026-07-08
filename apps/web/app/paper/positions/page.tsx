import { Card, DataTable, EmptyState, PageTitle } from "@/components/ResearchUI";
import { getPaperSnapshot } from "@/lib/paper";
import { money, number } from "@/lib/format";

export default async function PaperPositionsPage() {
  const snapshot = await getPaperSnapshot();
  return (
    <div className="pageStack">
      <PageTitle title="Paper Positions" description="Long-only simulated positions. Shorting and leverage are blocked." />
      <Card title="Position ledger" eyebrow={snapshot.account?.name ?? "No account"}>
        {snapshot.positions.length ? (
          <DataTable
            columns={["Symbol", "Quantity", "Average", "Last", "Market Value", "Unrealized PnL", "Realized PnL"]}
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
          <EmptyState title="No simulated positions." body="Paper fills will update this long-only ledger." />
        )}
      </Card>
    </div>
  );
}
