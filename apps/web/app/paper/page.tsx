import Link from "next/link";
import { CreatePaperAccount, PaperOperations } from "@/components/PaperActions";
import { Card, DataTable, EmptyState, LineChart, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getPaperSnapshot } from "@/lib/paper";
import { money, number } from "@/lib/format";

export default async function PaperDashboardPage() {
  const snapshot = await getPaperSnapshot();
  const equityValues = snapshot.equity.map((row) => Number(row.equity));
  return (
    <div className="pageStack">
      <PageTitle title="Paper Trading Dashboard" description="Internal simulation architecture for research deployments. No broker connection, live trading, leverage, or real order routing." />
      {snapshot.account ? (
        <>
          <section className="paperHero">
            <div><span className="eyebrow">SIMULATION CONTROL CENTER</span><h2>{snapshot.account.name}</h2><p>Run, inspect, and reconcile the full paper execution lifecycle from one workspace.</p></div>
            <PaperOperations accountId={snapshot.account.id} />
          </section>
          <div className="metricGrid">
            <MetricCard label="Equity" value={money(snapshot.balances?.equity)} detail="Simulated account value" />
            <MetricCard label="Cash" value={money(snapshot.balances?.cash_balance)} detail="Paper cash only" />
            <MetricCard label="Open positions" value={snapshot.positions.length} detail="Long-only simulated positions" />
            <MetricCard label="Deployments" value={snapshot.deployments.length} detail="Simulation lifecycle records" />
          </div>
          <div className="dashboardGrid">
            <Card title="Paper equity curve" eyebrow="Simulation">
              <LineChart values={equityValues.length ? equityValues : [Number(snapshot.balances?.equity ?? 0)]} label="Paper equity curve" />
            </Card>
            <Card title="Guardrails" eyebrow="Safety">
              <div className="scoreList">
                <span>Broker integration <strong>Disabled</strong></span>
                <span>Live order routing <strong>Disabled</strong></span>
                <span>Leverage <strong>Blocked</strong></span>
                <span>Execution venue <strong>Historical candles only</strong></span>
              </div>
            </Card>
          </div>
          <Card title="Recent paper orders" eyebrow={snapshot.account.name}>
            {snapshot.orders.length ? (
              <DataTable
                columns={["Order", "Symbol", "Side", "Qty", "Status", "Reason"]}
                rows={snapshot.orders.slice(0, 8).map((row) => [row.id, row.symbol, row.side, number(row.quantity), row.status, row.rejected_reason || "Filled or pending"])}
              />
            ) : (
              <EmptyState title="No paper orders yet." body="Submit a simulated order from the Paper Orders page." action={<Link className="button" href="/paper/orders">Open orders</Link>} />
            )}
          </Card>
          <Card title="Execution activity" eyebrow="Immutable simulation audit trail">
            {snapshot.logs.length ? (
              <div className="executionTimeline">{snapshot.logs.slice(0, 12).map((log) => <article key={log.id}><span className="eventDot" /><div><strong>{log.event_type.replaceAll("_", " ")}</strong><p>{log.message}</p><time>{new Date(log.created_at).toLocaleString()}</time></div></article>)}</div>
            ) : <EmptyState title="No execution events." body="Account, order, fill, cancellation, and reconciliation events will appear here." />}
          </Card>
        </>
      ) : (
        <Card title="Create paper account" eyebrow="Simulation only">
          <CreatePaperAccount />
        </Card>
      )}
    </div>
  );
}
