import Link from "next/link";
import { CreatePaperAccount, PaperOperations, PaperSchedulerControls, TslaPaperScanControls } from "@/components/PaperActions";
import { Card, DataTable, EmptyState, LineChart, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getPaperSnapshot } from "@/lib/paper";
import { money, number } from "@/lib/format";

export default async function PaperDashboardPage() {
  const snapshot = await getPaperSnapshot();
  const equityValues = snapshot.equity.map((row) => Number(row.equity));
  const tslaDeployment = snapshot.deployments.find((row) => row.symbol === "TSLA" && row.timeframe === "1h" && row.strategy_name === "momentum" && row.strategy_version === "bull_v2" && row.status === "active");
  const tslaOrders = snapshot.orders.filter((row) => row.symbol === "TSLA").slice(0, 6);
  const tslaPosition = snapshot.positions.find((row) => row.symbol === "TSLA");
  const tslaLogs = snapshot.logs
    .filter((log) => log.deployment_id === tslaDeployment?.id || JSON.stringify(log.payload ?? {}).includes("TSLA"))
    .slice(0, 8);
  const tslaEquityValues = equityValues.length ? equityValues.slice(-40) : [Number(snapshot.balances?.equity ?? 0)];
  const scheduler = snapshot.scheduler;
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
          <Card title="TSLA 1h momentum_bull_v2_007 paper deployment" eyebrow="Automated simulation scan">
            <div className="dashboardGrid wideLeft">
              <div className="scoreList">
                <span>Deployment <strong>{tslaDeployment ? "Active" : "Not created"}</strong></span>
                <span>Candidate <strong>momentum_bull_v2_007</strong></span>
                <span>Market <strong>TSLA / 1h</strong></span>
                <span>Mode <strong>Simulation only</strong></span>
                <span>Latest scan <strong>{tslaDeployment?.last_scan_at ? new Date(tslaDeployment.last_scan_at).toLocaleString() : "Never"}</strong></span>
                <span>Latest signal <strong>{tslaDeployment?.last_signal ?? "No scan yet"}</strong></span>
                <span>Latest result <strong>{tslaDeployment?.last_check_result ?? "Deploy, then run the paper scan."}</strong></span>
              </div>
              <div className="workflowStack">
                <TslaPaperScanControls accountId={snapshot.account.id} deploymentId={tslaDeployment?.id} />
                <div className="actionNote">
                  <strong>Scan cycle</strong>
                  <p>Syncs latest TSLA Alpaca 1h candles, recalculates features, evaluates the active deployment, creates simulated orders only on matching rules, processes candle fills, updates ledger state, and writes an execution log.</p>
                </div>
              </div>
            </div>
          </Card>
          <Card title="Local paper scan scheduler" eyebrow="Safe automation">
            <div className="dashboardGrid wideLeft">
              <div className="scoreList">
                <span>Enabled <strong>{scheduler ? (scheduler.enabled ? "Enabled" : "Disabled") : "Unavailable"}</strong></span>
                <span>Cadence <strong>{scheduler?.cadence ?? "Unknown"}</strong></span>
                <span>Running now <strong>{scheduler?.is_running ? "Yes" : "No"}</strong></span>
                <span>Last run <strong>{scheduler?.last_run_at ? new Date(scheduler.last_run_at).toLocaleString() : "Never"}</strong></span>
                <span>Next run <strong>{scheduler?.next_run_at ? new Date(scheduler.next_run_at).toLocaleString() : scheduler?.cadence === "manual" ? "Manual only" : "Not scheduled"}</strong></span>
                <span>Latest result <strong>{scheduler?.latest_result ?? "No scheduled result yet"}</strong></span>
                <span>Latest error <strong>{scheduler?.latest_error ?? "None"}</strong></span>
              </div>
              <div className="workflowStack">
                {scheduler ? <PaperSchedulerControls enabled={scheduler.enabled} cadence={scheduler.cadence} /> : null}
                <div className="actionNote">
                  <strong>Automation boundary</strong>
                  <p>The scheduler only calls the internal simulation scan for active simulation-only deployments. It does not submit broker orders or enable live trading. Manual Run Scan remains available above.</p>
                </div>
              </div>
            </div>
          </Card>
          <div className="dashboardGrid">
            <Card title="TSLA paper position" eyebrow="PnL">
              {tslaPosition ? (
                <div className="scoreList">
                  <span>Quantity <strong>{number(tslaPosition.quantity)}</strong></span>
                  <span>Average price <strong>{money(tslaPosition.average_price)}</strong></span>
                  <span>Last price <strong>{money(tslaPosition.last_price)}</strong></span>
                  <span>Market value <strong>{money(tslaPosition.market_value)}</strong></span>
                  <span>Unrealized PnL <strong>{money(tslaPosition.unrealized_pnl)}</strong></span>
                  <span>Realized PnL <strong>{money(tslaPosition.realized_pnl)}</strong></span>
                </div>
              ) : (
                <EmptyState title="No TSLA paper position." body="The scan will create a simulated long position only when the candidate rules match." />
              )}
            </Card>
            <Card title="TSLA equity curve" eyebrow="Paper account">
              <LineChart values={tslaEquityValues} label="Paper equity after TSLA scan activity" />
            </Card>
          </div>
          <Card title="TSLA paper orders" eyebrow="Deployment orders">
            {tslaOrders.length ? (
              <DataTable
                columns={["Order", "Side", "Type", "Qty", "Status", "Protective / Reason"]}
                rows={tslaOrders.map((row) => [row.id, row.side, row.order_type, number(row.quantity), row.status, row.rejected_reason || row.trigger_price || row.stop_loss_price || row.take_profit_price || "Filled or pending"])}
              />
            ) : (
              <EmptyState title="No TSLA paper orders." body="Run the paper scan after deployment. Orders are simulated only when strategy rules match." />
            )}
          </Card>
          <Card title="TSLA execution logs" eyebrow="Scan audit trail">
            {tslaLogs.length ? (
              <div className="executionTimeline">{tslaLogs.map((log) => <article key={log.id}><span className="eventDot" /><div><strong>{log.event_type.replaceAll("_", " ")}</strong><p>{log.message}</p><time>{new Date(log.created_at).toLocaleString()}</time></div></article>)}</div>
            ) : <EmptyState title="No TSLA scan logs." body="Deployment creation, skipped decisions, simulated orders, fills, and reconciliation checks will appear here." />}
          </Card>
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
