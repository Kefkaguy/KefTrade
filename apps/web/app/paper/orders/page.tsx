import { CreatePaperOrder } from "@/components/PaperActions";
import { Card, DataTable, EmptyState, PageTitle } from "@/components/ResearchUI";
import { getPaperSnapshot } from "@/lib/paper";
import { money, number } from "@/lib/format";

export default async function PaperOrdersPage() {
  const snapshot = await getPaperSnapshot();
  return (
    <div className="pageStack">
      <PageTitle title="Paper Orders" description="Submit and review simulated orders. Fills are generated only from candle data." />
      {snapshot.account ? (
        <>
          <Card title="Submit simulated order" eyebrow="No real routing">
            <CreatePaperOrder accountId={snapshot.account.id} />
          </Card>
          <Card title="Orders" eyebrow={snapshot.account.name}>
            {snapshot.orders.length ? (
              <DataTable
                columns={["ID", "Symbol", "Side", "Type", "Qty", "Limit", "Status", "Rejected reason"]}
                rows={snapshot.orders.map((row) => [
                  row.id,
                  row.symbol,
                  row.side,
                  row.order_type,
                  number(row.quantity),
                  row.limit_price ? money(row.limit_price) : "N/A",
                  row.status,
                  row.rejected_reason || "N/A"
                ])}
              />
            ) : (
              <EmptyState title="No orders." body="Submit a small simulated order after candle data exists for that symbol." />
            )}
          </Card>
          <Card title="Fills" eyebrow="Candle simulation">
            {snapshot.fills.length ? (
              <DataTable
                columns={["Fill", "Order", "Symbol", "Side", "Qty", "Price", "Fee"]}
                rows={snapshot.fills.map((row) => [row.id, row.order_id, row.symbol, row.side, number(row.quantity), money(row.fill_price), money(row.fee)])}
              />
            ) : (
              <EmptyState title="No fills." body="Marketable simulated orders will create fills from latest candle prices." />
            )}
          </Card>
        </>
      ) : (
        <EmptyState title="No paper account." body="Create a paper account from the Paper Dashboard first." />
      )}
    </div>
  );
}
