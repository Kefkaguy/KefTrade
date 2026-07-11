import { CancelOrderButton, CreatePaperOrder, PaperOperations } from "@/components/PaperActions";
import { DataTable, EmptyState, PageTitle } from "@/components/ResearchUI";
import { getPaperSnapshot } from "@/lib/paper";
import { money, number } from "@/lib/format";

export default async function PaperOrdersPage() {
  const snapshot = await getPaperSnapshot();
  return (
    <div className="pageStack">
      <PageTitle title="Paper Orders" description="Submit and review simulated orders. Fills are generated only from candle data." />
      {snapshot.account ? (
        <>
          <section className="simulationBoundary">
            <div><span className="boundaryPulse" /><strong>Internal simulation</strong><small>No broker connection · no live routing · no leverage</small></div>
            <PaperOperations accountId={snapshot.account.id} />
          </section>
          <div className="orderStudio">
            <section className="orderBlotter panel">
              <header className="blotterHeader"><div><span className="sectionLabel">Order blotter</span><h2>Execution queue</h2></div><div className="blotterStats"><span>Account <strong>{snapshot.account.name}</strong></span><span>Orders <strong>{snapshot.orders.length}</strong></span></div></header>
            {snapshot.orders.length ? (
              <DataTable
                columns={["ID", "Symbol", "Side", "Type", "Qty", "Trigger / Limit", "Status", "Action"]}
                rows={snapshot.orders.map((row) => [
                  row.id,
                  <strong key={`${row.id}-symbol`}>{row.symbol}</strong>,
                  <span className={`status ${row.side === "buy" ? "setup" : "avoid"}`} key={`${row.id}-side`}>{row.side}</span>,
                  row.order_type,
                  number(row.quantity),
                  row.trigger_price ? money(row.trigger_price) : row.limit_price ? money(row.limit_price) : "Market",
                  <span className="status" key={`${row.id}-status`}>{row.status}</span>,
                  row.status === "pending" ? <CancelOrderButton key={row.id} orderId={row.id} /> : row.rejected_reason || "—"
                ])}
              />
            ) : <EmptyState title="The blotter is clear." body="Compose a simulated order in the ticket. Marketable orders fill from stored candle data." />}
            </section>
            <aside className="tradeTicket"><header><span className="sectionLabel">Trade ticket / simulation</span><h2>Compose order</h2><p>Set an entry and optional protective exits. Nothing leaves KefTrade.</p></header><CreatePaperOrder accountId={snapshot.account.id} /></aside>
          </div>
          <section className="panel fillLedger">
            <div className="panelHeader"><div><span className="sectionLabel">Candle simulation</span><h2>Fill ledger</h2></div><span className="ledgerCount">{snapshot.fills.length.toString().padStart(2, "0")}</span></div>
            {snapshot.fills.length ? (
              <DataTable
                columns={["Fill", "Order", "Symbol", "Side", "Qty", "Price", "Fee"]}
                rows={snapshot.fills.map((row) => [row.id, row.order_id, row.symbol, row.side, number(row.quantity), money(row.fill_price), money(row.fee)])}
              />
            ) : (
              <EmptyState title="No fills." body="Marketable simulated orders will create fills from latest candle prices." />
            )}
          </section>
        </>
      ) : (
        <EmptyState title="No paper account." body="Create a paper account from the Paper Dashboard first." />
      )}
    </div>
  );
}
