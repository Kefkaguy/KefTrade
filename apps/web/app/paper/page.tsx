import Link from "next/link";
import { AcknowledgeAlertButton, CreatePaperAccount, EvidenceNotificationControls, PaperOperations, PaperSchedulerControls, SignalReviewControls, TslaPaperScanControls } from "@/components/PaperActions";
import { Card, DataTable, EmptyState, LineChart, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getPaperSnapshot } from "@/lib/paper";
import { money, number } from "@/lib/format";

export default async function PaperDashboardPage() {
  const snapshot = await getPaperSnapshot();
  const equityValues = snapshot.equity.map((row) => Number(row.equity));
  const tslaDeployment = snapshot.deployments.find((row) => row.symbol === "TSLA" && row.timeframe === "1h" && row.strategy_name === "momentum" && row.strategy_version === "bull_v2" && row.status === "active");
  const tslaOrders = snapshot.orders.filter((row) => row.symbol === "TSLA");
  const latestTslaOrder = tslaOrders[0] ?? null;
  const tslaFills = snapshot.fills.filter((row) => row.symbol === "TSLA");
  const latestTslaFill = tslaFills[0] ?? null;
  const tslaPosition = snapshot.positions.find((row) => row.symbol === "TSLA");
  const allTslaLogs = snapshot.logs
    .filter((log) => log.deployment_id === tslaDeployment?.id || JSON.stringify(log.payload ?? {}).includes("TSLA"));
  const tslaLogs = allTslaLogs.slice(0, 12);
  const tslaEquityValues = equityValues.length ? equityValues.slice(-40) : [Number(snapshot.balances?.equity ?? 0)];
  const scheduler = snapshot.scheduler;
  const tslaAlerts = snapshot.alerts.filter((alert) => alert.symbol === "TSLA" || alert.symbol === "SYSTEM").slice(0, 12);
  const tslaSignalReviews = snapshot.signalReviews.filter((review) => review.symbol === "TSLA" && review.timeframe === "1h").slice(0, 8);
  const latestSignalReview = tslaSignalReviews[0] ?? null;
  const latestAlert = tslaAlerts[0] ?? null;
  const unacknowledgedAlerts = tslaAlerts.filter((alert) => !alert.acknowledged_at);
  const duplicateSkips = allTslaLogs.filter((log) => log.event_type === "paper_scan_duplicate_candle_skipped").length;
  const scanPayload = tslaDeployment?.last_scan_payload && typeof tslaDeployment.last_scan_payload === "object" ? tslaDeployment.last_scan_payload as Record<string, unknown> : null;
  const syncPayload = objectValue(scanPayload, "sync");
  const lastScannedCandle = tslaDeployment?.last_scanned_candle_timestamp ?? null;
  const latestStoredCandle = primitiveValue(syncPayload, "last_timestamp") ?? primitiveValue(scanPayload, "candle_timestamp") ?? lastScannedCandle;
  const candleAge = candleAgeLabel(latestStoredCandle);
  const staleCandle = isStaleCandle(latestStoredCandle);
  const latestDecision = objectValue(scanPayload, "decision");
  const latestDecisionText = latestDecision ? [
    labelValue("Signal", textValue(latestDecision, "signal")),
    labelValue("Stop", textValue(latestDecision, "stop_loss")),
    labelValue("Target", textValue(latestDecision, "take_profit")),
    labelValue("Risk/reward", textValue(latestDecision, "risk_reward"))
  ].filter(Boolean).join(" / ") : tslaDeployment?.last_signal ?? "No scan yet";
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
            <MetricCard label="Account positions" value={snapshot.positions.length} detail="All symbols, long-only simulation" />
            <MetricCard label="Deployments" value={snapshot.deployments.length} detail="Simulation lifecycle records" />
          </div>
          <Card title="TSLA simulation health dashboard" eyebrow="Paper Lab monitor">
            <div className="metricGrid">
              <MetricCard label="Scheduler" value={scheduler?.enabled ? "Enabled" : "Disabled"} detail={`${scheduler?.cadence ?? "unknown"} cadence`} tone={scheduler?.enabled ? "success" : "warning"} />
              <MetricCard label="Last success" value={formatDate(scheduler?.last_run_at)} detail={scheduler?.latest_result ?? "No scheduled result yet"} />
              <MetricCard label="Next scan" value={formatDate(scheduler?.next_run_at)} detail={scheduler?.cadence === "manual" ? "Manual only" : "Local scheduler"} />
              <MetricCard label="Duplicate skips" value={duplicateSkips} detail="Same completed candle blocked" tone={duplicateSkips ? "warning" : "success"} />
              <MetricCard label="Latest stored candle" value={formatDate(latestStoredCandle)} detail={candleAge} tone={staleCandle ? "warning" : "success"} />
              <MetricCard label="TSLA position" value={tslaPosition ? number(tslaPosition.quantity) : "0"} detail={tslaPosition ? "Current simulated TSLA quantity" : "No TSLA exposure"} />
              <MetricCard label="Unrealized PnL" value={money(tslaPosition?.unrealized_pnl)} detail="TSLA mark-to-market simulation" />
              <MetricCard label="Realized PnL" value={money(tslaPosition?.realized_pnl)} detail="Closed TSLA simulated PnL" />
            </div>
            <div className="dashboardGrid wideLeft">
              <div className="scoreList">
                <span>Deployment <strong>{tslaDeployment ? "Active" : "Not created"}</strong></span>
                <span>Candidate <strong>momentum_bull_v2_007</strong></span>
                <span>Market <strong>TSLA / 1h</strong></span>
                <span>Mode <strong>Simulation only</strong></span>
                <span>Last successful scan <strong>{formatDate(tslaDeployment?.last_scan_at)}</strong></span>
                <span>Next scan <strong>{formatDate(scheduler?.next_run_at)}</strong></span>
                <span>Last scanned candle <strong>{formatDate(lastScannedCandle)}</strong></span>
                <span>Latest stored candle <strong>{formatDate(latestStoredCandle)}</strong></span>
                <span>Data freshness <strong>{candleAge}</strong></span>
                <span>Latest strategy decision <strong>{latestDecisionText}</strong></span>
                <span>Latest result <strong>{tslaDeployment?.last_check_result ?? "Deploy, then run the paper scan."}</strong></span>
                <span>Latest error <strong>{scheduler?.latest_error ?? "None"}</strong></span>
              </div>
              <div className="workflowStack">
                <TslaPaperScanControls accountId={snapshot.account.id} deploymentId={tslaDeployment?.id} />
                <div className="actionNote">
                  <strong>Scan cycle</strong>
                  <p>Syncs latest TSLA Alpaca 1h candles, recalculates features, blocks stale data before setup evaluation, creates simulated orders only on matching rules, processes candle fills from fresh stored candles, updates ledger state, and writes an execution log.</p>
                </div>
              </div>
            </div>
          </Card>
          <Card title="Evidence Alert Dashboard" eyebrow="Research-only alerts">
            <div className="metricGrid">
              <MetricCard label="Active alerts" value={unacknowledgedAlerts.length} detail="Unacknowledged research-only notices" tone={unacknowledgedAlerts.length ? "warning" : "success"} />
              <MetricCard label="Latest verdict" value={latestAlert?.verdict ?? "No Setup"} detail={latestAlert?.alert_type?.replaceAll("_", " ") ?? "No alert yet"} />
              <MetricCard label="Evidence score" value={evidenceScore(latestAlert)} detail="Matched rules minus failed rules" />
              <MetricCard label="Current regime" value={latestAlert?.regime ?? "Unknown"} detail="From latest alert context" />
            </div>
            {latestAlert ? (
              <div className="dashboardGrid wideLeft">
                <div className="scoreList">
                  <span>Latest alert <strong>{latestAlert.verdict}</strong></span>
                  <span>Severity <strong>{latestAlert.severity}</strong></span>
                  <span>Why alert fired <strong>{latestAlert.matched_rules.length ? latestAlert.matched_rules.join(" / ") : "No matched setup rules."}</strong></span>
                  <span>Why no alert fired <strong>{latestAlert.failed_rules.length ? latestAlert.failed_rules.join(" / ") : "All alert gates passed."}</strong></span>
                  <span>Historical PF <strong>{formatMaybeNumber(latestAlert.profit_factor)}</strong></span>
                  <span>Expectancy <strong>{formatMaybeNumber(latestAlert.expectancy)}</strong></span>
                  <span>Trade count <strong>{latestAlert.trade_count ?? "n/a"}</strong></span>
                  <span>Max drawdown <strong>{formatMaybeNumber(latestAlert.max_drawdown)}</strong></span>
                  <span>Candle <strong>{formatDate(latestAlert.candle_timestamp)}</strong></span>
                  <span>Disclaimer <strong>Research-only. Not financial advice. No trade is executed.</strong></span>
                </div>
                <div className="workflowStack">
                  {!latestAlert.acknowledged_at ? <AcknowledgeAlertButton alertId={latestAlert.id} /> : <div className="actionNote"><strong>Acknowledged</strong><p>{formatDate(latestAlert.acknowledged_at)}</p></div>}
                  <div className="actionNote">
                    <strong>Notification options</strong>
                    <p>In-app alerts are always active. Browser notifications are optional and local to this browser. Email, Discord, and Telegram remain intentionally deferred options.</p>
                  </div>
                  <div className="actionNote">
                    <strong>AI boundary</strong>
                    <p>Ask Kef may explain alerts from stored evidence only. It cannot place trades or route orders.</p>
                  </div>
                </div>
              </div>
            ) : (
              <EmptyState title="No evidence alerts yet." body="Alerts will appear after paper scans or research scans create research-only evidence notices." />
            )}
          </Card>
          <Card title="Signal Review Dashboard" eyebrow="Human review layer">
            {latestSignalReview ? (
              <div className="dashboardGrid wideLeft">
                <div className="workflowStack">
                  <div className="metricGrid">
                    <MetricCard label="Status" value={latestSignalReview.status} detail={latestSignalReview.verdict} tone={latestSignalReview.status === "Stale Data Blocked" ? "warning" : latestSignalReview.status === "Setup Worth Reviewing" ? "success" : "neutral"} />
                    <MetricCard label="Evidence score" value={latestSignalReview.evidence_score} detail="Matched rules / total checks" />
                    <MetricCard label="Current regime" value={latestSignalReview.regime ?? "Unknown"} detail="Latest candle context" />
                    <MetricCard label="Data freshness" value={latestSignalReview.data_freshness} detail={formatDate(latestSignalReview.latest_candle_timestamp)} tone={latestSignalReview.status === "Stale Data Blocked" ? "warning" : "success"} />
                  </div>
                  <div className="scoreList">
                    <span>Symbol <strong>{latestSignalReview.symbol}</strong></span>
                    <span>Timeframe <strong>{latestSignalReview.timeframe}</strong></span>
                    <span>Strategy <strong>{latestSignalReview.strategy_id}</strong></span>
                    <span>Verdict <strong>{latestSignalReview.verdict}</strong></span>
                    <span>Historical profit factor <strong>{formatMaybeNumber(latestSignalReview.profit_factor)}</strong></span>
                    <span>Expectancy <strong>{formatMaybeNumber(latestSignalReview.expectancy)}</strong></span>
                    <span>Trade count <strong>{latestSignalReview.trade_count ?? "n/a"}</strong></span>
                    <span>Max drawdown <strong>{formatMaybeNumber(latestSignalReview.max_drawdown)}</strong></span>
                    <span>Latest candle timestamp <strong>{formatDate(latestSignalReview.latest_candle_timestamp)}</strong></span>
                  </div>
                  <div className="dashboardGrid">
                    <div className="actionNote">
                      <strong>Matched rules</strong>
                      {latestSignalReview.matched_rules.length ? <ul>{latestSignalReview.matched_rules.map((rule) => <li key={rule}>{rule}</li>)}</ul> : <p>No matched setup rules yet.</p>}
                    </div>
                    <div className="actionNote">
                      <strong>Failed rules</strong>
                      {latestSignalReview.failed_rules.length ? <ul>{latestSignalReview.failed_rules.map((rule) => <li key={rule}>{rule}</li>)}</ul> : <p>No failed rules recorded.</p>}
                    </div>
                  </div>
                  <div className="metricGrid">
                    <MetricCard label="Possible Entry Zone" value={moneyMaybe(latestSignalReview.possible_entry_price)} detail="Latest close when setup matches" />
                    <MetricCard label="Invalidation Level" value={moneyMaybe(latestSignalReview.invalidation_level)} detail="Below recent swing low" />
                    <MetricCard label="Risk Target" value={moneyMaybe(latestSignalReview.risk_target)} detail="Configured risk/reward target" />
                    <MetricCard label="Exit Zone" value={moneyMaybe(latestSignalReview.exit_zone)} detail="Research-only exit reference" />
                    <MetricCard label="Risk per share" value={moneyMaybe(latestSignalReview.risk_per_share)} detail="Possible entry minus invalidation" />
                    <MetricCard label="Reward per share" value={moneyMaybe(latestSignalReview.reward_per_share)} detail="Risk target minus possible entry" />
                    <MetricCard label="Risk/reward ratio" value={formatMaybeNumber(latestSignalReview.risk_reward_ratio)} detail="Reward divided by risk" />
                    <MetricCard label="Max holding bars exit" value={latestSignalReview.max_holding_bars ?? "n/a"} detail="Strategy time-exit setting" />
                  </div>
                  {latestSignalReview.note ? <div className="actionNote"><strong>Review note</strong><p>{latestSignalReview.note}</p></div> : null}
                  <p className="formHint">{latestSignalReview.disclaimer}</p>
                </div>
                <SignalReviewControls review={latestSignalReview} deploymentId={tslaDeployment?.id} />
              </div>
            ) : (
              <div className="dashboardGrid wideLeft">
                <EmptyState title="No Signal Review yet." body="Run the TSLA paper scan or refresh Signal Review after the TSLA simulation deployment exists." />
                <SignalReviewControls review={null} deploymentId={tslaDeployment?.id} />
              </div>
            )}
          </Card>
          <Card title="Recent evidence alerts" eyebrow="In-app notifications first">
            {tslaAlerts.length ? (
              <div className="executionTimeline">
                {tslaAlerts.map((alert) => (
                  <article key={alert.id}>
                    <span className="eventDot" />
                    <div>
                      <strong>{alert.verdict}</strong>
                      <p>{alert.evidence_summary}</p>
                      <p>{alert.alert_type.replaceAll("_", " ")} / {alert.severity} / {alert.acknowledged_at ? "acknowledged" : "active"}</p>
                      <time>{new Date(alert.created_at).toLocaleString()}</time>
                    </div>
                  </article>
                ))}
              </div>
            ) : <EmptyState title="No alert history." body="Research-only alert cards will show here after scan evidence is evaluated." />}
          </Card>
          <Card title="Browser evidence notifications" eyebrow="Optional local browser alerts">
            <EvidenceNotificationControls alerts={tslaAlerts} />
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
                  <span>Cash <strong>{money(snapshot.balances?.cash_balance)}</strong></span>
                  <span>Unrealized PnL <strong>{money(tslaPosition.unrealized_pnl)}</strong></span>
                  <span>Realized PnL <strong>{money(tslaPosition.realized_pnl)}</strong></span>
                </div>
              ) : (
                <EmptyState title="No TSLA paper position." body={`The account has ${snapshot.positions.length} open simulated position(s) across all symbols, but none for TSLA. The TSLA scan will create exposure only when candidate rules match.`} />
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
                rows={tslaOrders.slice(0, 8).map((row) => [row.id, row.side, row.order_type, number(row.quantity), row.status, row.rejected_reason || row.trigger_price || row.stop_loss_price || row.take_profit_price || "Filled or pending"])}
              />
            ) : (
              <EmptyState title="No TSLA paper orders." body="Run the paper scan after deployment. Orders are simulated only when strategy rules match." />
            )}
          </Card>
          <div className="dashboardGrid">
            <Card title="Latest TSLA order" eyebrow="Order monitor">
              {latestTslaOrder ? (
                <div className="scoreList">
                  <span>Order ID <strong>{latestTslaOrder.id}</strong></span>
                  <span>Side/type <strong>{latestTslaOrder.side} / {latestTslaOrder.order_type}</strong></span>
                  <span>Quantity <strong>{number(latestTslaOrder.quantity)}</strong></span>
                  <span>Status <strong>{latestTslaOrder.status}</strong></span>
                  <span>Submitted <strong>{formatDate(latestTslaOrder.submitted_at)}</strong></span>
                  <span>Filled <strong>{formatDate(latestTslaOrder.filled_at)}</strong></span>
                </div>
              ) : (
                <EmptyState title="No TSLA order yet." body="The deployment has not created a simulated TSLA order." />
              )}
            </Card>
            <Card title="Latest TSLA fill" eyebrow="Fill monitor">
              {latestTslaFill ? (
                <div className="scoreList">
                  <span>Fill ID <strong>{latestTslaFill.id}</strong></span>
                  <span>Order ID <strong>{latestTslaFill.order_id}</strong></span>
                  <span>Side <strong>{latestTslaFill.side}</strong></span>
                  <span>Quantity <strong>{number(latestTslaFill.quantity)}</strong></span>
                  <span>Fill price <strong>{money(latestTslaFill.fill_price)}</strong></span>
                  <span>Fee <strong>{money(latestTslaFill.fee)}</strong></span>
                  <span>Filled at <strong>{formatDate(latestTslaFill.filled_at)}</strong></span>
                </div>
              ) : (
                <EmptyState title="No TSLA fill yet." body="Fills appear only after simulated candle execution." />
              )}
            </Card>
          </div>
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

function formatDate(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "Never";
}

function objectValue(source: unknown, key: string): Record<string, unknown> | null {
  if (!source || typeof source !== "object") return null;
  const value = (source as Record<string, unknown>)[key];
  return value && typeof value === "object" ? value as Record<string, unknown> : null;
}

function textValue(source: Record<string, unknown>, key: string) {
  const value = source[key];
  return value === null || value === undefined ? "" : String(value);
}

function primitiveValue(source: Record<string, unknown> | null, key: string) {
  if (!source) return null;
  const value = source[key];
  return value === null || value === undefined || typeof value === "object" ? null : String(value);
}

function labelValue(label: string, value: string) {
  return value ? `${label}: ${value}` : "";
}

function evidenceScore(alert: { matched_rules?: unknown[]; failed_rules?: unknown[] } | null) {
  if (!alert) return "0/0";
  return `${alert.matched_rules?.length ?? 0}/${(alert.matched_rules?.length ?? 0) + (alert.failed_rules?.length ?? 0)}`;
}

function formatMaybeNumber(value?: string | number | null) {
  if (value === null || value === undefined || value === "") return "n/a";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(4) : String(value);
}

function moneyMaybe(value?: string | number | null) {
  if (value === null || value === undefined || value === "") return "n/a";
  return money(value);
}

function candleAgeLabel(value?: string | null) {
  if (!value) return "No completed candle scanned yet";
  const ageMs = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(ageMs) || ageMs < 0) return "Timestamp pending verification";
  const hours = Math.floor(ageMs / (1000 * 60 * 60));
  if (hours < 48) return `${hours}h old`;
  const days = Math.floor(hours / 24);
  return `${days}d old`;
}

function isStaleCandle(value?: string | null) {
  if (!value) return true;
  const ageMs = Date.now() - new Date(value).getTime();
  return !Number.isFinite(ageMs) || ageMs > 2 * 24 * 60 * 60 * 1000;
}
