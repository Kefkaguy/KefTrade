import Link from "next/link";
import { AlertTriangle, ArrowUpRight, ShieldCheck } from "lucide-react";
import { BulkDeploymentControls, DeploymentControlPanel } from "@/components/DeploymentManagementActions";
import { Card, DataTable, EmptyState, LineChart, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getDeploymentManagement, getMissionControl, getResearchIntelligence, type DeploymentManagementSnapshot, type MissionControlSnapshot, type MissionControlStatus } from "@/lib/api";
import { money, number } from "@/lib/format";

export default async function MissionControlPage() {
  const [snapshot, deploymentManagement] = await Promise.all([
    getMissionControl().catch((error) => ({ error: error instanceof Error ? error.message : "Mission Control unavailable" })),
    getDeploymentManagement().catch((error) => ({ error: error instanceof Error ? error.message : "Deployment management unavailable" }))
  ]);
  if ("error" in snapshot) {
    return (
      <div className="pageStack">
        <PageTitle title="Research Mission Control" description="Multi-asset research operations dashboard." />
        <Card title="Mission Control unavailable" eyebrow="Full error">
          <EmptyState title="Unable to load dashboard data." body={snapshot.error} action={<Link className="button" href="/paper">Open Paper Lab</Link>} />
        </Card>
      </div>
    );
  }

  const equityValues = snapshot.paper_account.recent_equity_curve.map((row) => Number(row.equity)).filter(Number.isFinite);
  const deploymentCenter = "error" in deploymentManagement ? null : deploymentManagement;
  const researchIntelligence = await getResearchIntelligence().catch(() => null);
  return (
    <div className="pageStack missionControl">
      <PageTitle
        title="Research Mission Control"
        description="Operational overview for research activity, simulation deployments, scheduler health, alerts, market data freshness, and paper-only portfolio state."
        actions={<Link className="button" href="/paper">Open Paper Lab</Link>}
      />

      <section className="paperHero missionHero">
        <div>
          <span className="eyebrow">SIMULATION OPERATIONS</span>
          <h2>{snapshot.system_health.overall_status}</h2>
          <p>{snapshot.system_health.simulation_safety_status}</p>
        </div>
        <div className="safetyStack" aria-label="Research safety status">
          <span><ShieldCheck size={16} /> Simulation protected</span>
          <span>Live routing is physically disabled</span>
          <span>No broker order API is used by this dashboard</span>
        </div>
      </section>

      {snapshot.subsystem_errors.length ? (
        <Card title="Partial dashboard data" eyebrow="Subsystem warnings">
          <div className="warningList">
            {snapshot.subsystem_errors.map((error) => <span key={`${error.subsystem}-${error.error}`}><AlertTriangle size={14} /> {error.subsystem}: {error.error}</span>)}
          </div>
        </Card>
      ) : null}

      {deploymentCenter ? <DeploymentManagementCenter snapshot={deploymentCenter} /> : (
        <Card title="Deployment Control Center unavailable" eyebrow="Subsystem warning">
          <EmptyState title="Unable to load deployment management." body={"error" in deploymentManagement ? deploymentManagement.error : "Unknown deployment management error."} />
        </Card>
      )}

      <Card title="Research Intelligence" eyebrow="Stored evidence ranking" action={<Link className="tableLink" href="/research-intelligence">Open full dashboard <ArrowUpRight size={12} /></Link>}>
        {researchIntelligence ? (
          <div className="dashboardGrid wideLeft">
            <DataTable
              columns={["Rank", "Candidate", "Score", "Classification", "Priority", "Change"]}
              rows={(researchIntelligence.rankings ?? []).slice(0, 3).map((row) => [
                String(row.global_rank ?? "n/a"),
                `${String(row.symbol ?? "n/a")} / ${String(row.timeframe ?? "n/a")} / ${String(row.strategy ?? "n/a")}`,
                formatMaybeNumber(row.research_score as string | number | null),
                String(row.classification ?? "n/a"),
                String(row.review_priority ?? "n/a"),
                row.rank_change === null || row.rank_change === undefined ? "new/unchanged" : String(row.rank_change)
              ])}
            />
            <div className="scoreList">
              <SummaryLine label="Top review priority" value={String(researchIntelligence.review_priorities?.[0]?.candidate_id ?? "none")} />
              <SummaryLine label="Strongest strategy" value={String(researchIntelligence.summary.top_ranked_strategy ?? "none")} />
              <SummaryLine label="Stale ranking blocks" value={String(researchIntelligence.summary.stale_candidate_count ?? 0)} />
              <SummaryLine label="Concentration warning" value={String(researchIntelligence.portfolio_intelligence?.warnings?.[0] ?? "none")} />
            </div>
          </div>
        ) : <EmptyState title="Research Intelligence unavailable." body="Mission Control remains available with operations data; open the full dashboard after rankings load." />}
      </Card>

      <Card title="System Status Header" eyebrow="Current state">
        <div className="metricGrid">
          <MetricCard label="Research engine" value={snapshot.system_health.research_engine_status} tone={tone(snapshot.system_health.research_engine_status)} />
          <MetricCard label="Scheduler" value={snapshot.system_health.scheduler_status} detail={`${snapshot.system_health.scheduler_cadence ?? "unknown"} cadence`} tone={tone(snapshot.system_health.scheduler_status)} />
          <MetricCard label="Last successful scan" value={formatDate(snapshot.system_health.last_successful_scan)} detail="Deployment scan timestamp" />
          <MetricCard label="Next scheduled scan" value={formatDate(snapshot.system_health.next_scheduled_scan)} detail="Local simulation scheduler" />
          <MetricCard label="Latest completed candle" value={formatDate(snapshot.system_health.latest_completed_candle)} detail={snapshot.system_health.overall_data_freshness} tone={tone(snapshot.system_health.overall_data_freshness)} />
          <MetricCard label="Active deployments" value={snapshot.system_health.active_deployment_count} detail="Simulation-only" />
          <MetricCard label="Unacknowledged alerts" value={snapshot.system_health.unacknowledged_alert_count} tone={snapshot.system_health.unacknowledged_alert_count ? "warning" : "success"} />
          <MetricCard label="Safety status" value="Protected" detail="Simulation protected / live routing physically disabled" tone="success" />
        </div>
      </Card>

      <Card title="Research Summary Cards" eyebrow="Stored totals">
        <div className="metricGrid">
          <MetricCard label="Assets monitored" value={metric(snapshot, "assets_monitored")} />
          <MetricCard label="Active deployments" value={metric(snapshot, "active_deployments")} />
          <MetricCard label="Research opportunities" value={metric(snapshot, "research_opportunities")} />
          <MetricCard label="Setups requiring review" value={metric(snapshot, "setups_requiring_review")} tone={Number(metric(snapshot, "setups_requiring_review")) ? "warning" : "neutral"} />
          <MetricCard label="No-setup results" value={metric(snapshot, "no_setup_results")} />
          <MetricCard label="Stale-data blocks" value={metric(snapshot, "stale_data_blocks")} tone={Number(metric(snapshot, "stale_data_blocks")) ? "warning" : "success"} />
          <MetricCard label="Scheduler failures" value={metric(snapshot, "scheduler_failures")} tone={Number(metric(snapshot, "scheduler_failures")) ? "error" : "success"} />
          <MetricCard label="Open simulated positions" value={metric(snapshot, "open_simulated_positions")} />
          <MetricCard label="Paper equity" value={money(metric(snapshot, "total_paper_account_equity"))} detail="Simulated" />
          <MetricCard label="Unrealized PnL" value={money(metric(snapshot, "total_unrealized_pnl"))} detail="Simulated" />
          <MetricCard label="Realized PnL" value={money(metric(snapshot, "total_realized_pnl"))} detail="Simulated" />
        </div>
      </Card>

      <Card title="Multi-Asset Research Table" eyebrow="Monitored assets">
        {snapshot.assets.length ? (
          <DataTable
            columns={["Asset", "Strategy", "Status", "Verdict", "Evidence", "PF", "Expectancy", "Trades", "Drawdown", "Regime", "Candle", "Age", "Alert", "Position", "Sim PnL", "Open"]}
            rows={snapshot.assets.map((asset) => [
              <Link key="asset" className="assetLink" href={asset.links.asset_research}>{asset.symbol} <small>{asset.asset_class} / {asset.timeframe}</small></Link>,
              asset.selected_strategy,
              <StatusBadge key="status" status={asset.status} />,
              asset.latest_verdict,
              asset.evidence_score,
              formatMaybeNumber(asset.profit_factor),
              formatMaybeNumber(asset.expectancy),
              asset.trade_count ?? "n/a",
              formatMaybeNumber(asset.max_drawdown),
              asset.current_regime ?? "unknown",
              formatDate(asset.latest_candle_timestamp),
              asset.data_age_hours === null || asset.data_age_hours === undefined ? asset.data_freshness_detail : `${asset.data_age_hours.toFixed(1)}h`,
              asset.alert_severity ?? "none",
              asset.paper_position_status.replaceAll("_", " "),
              money(asset.simulated_unrealized_pnl),
              <Link key="open" className="tableLink" href={asset.links.signal_review}>Review <ArrowUpRight size={12} /></Link>
            ])}
          />
        ) : (
          <EmptyState title="No monitored assets yet." body="Mission Control will show symbols, deployments, alerts, signal reviews, and simulated positions when stored data exists." />
        )}
      </Card>

      <div className="dashboardGrid wideLeft">
        <Card title="Research Review Queue" eyebrow="Priority order">
          {snapshot.review_queue.length ? (
            <div className="executionTimeline compactTimeline">
              {snapshot.review_queue.map((item, index) => (
                <article key={`${item.symbol}-${item.reason}-${item.timestamp}-${index}`}>
                  <span className="eventDot" />
                  <div>
                    <strong>{item.symbol} / {item.reason}</strong>
                    <p>{item.strategy} / {item.current_verdict} / {item.severity}</p>
                    <time>{formatDate(item.timestamp)}</time>
                    <Link className="tableLink" href={item.action.href}>{item.action.label} <ArrowUpRight size={12} /></Link>
                  </div>
                </article>
              ))}
            </div>
          ) : <EmptyState title="Review queue is empty." body="No scheduler errors, stale data blocks, active setups, exit-risk reviews, or unacknowledged alerts are currently stored." />}
        </Card>

        <Card title="Daily Research Summary" eyebrow={String(snapshot.daily_summary.label ?? "Today")}>
          <div className="scoreList">
            <SummaryLine label="Scans completed" value={snapshot.daily_summary.scans_completed} />
            <SummaryLine label="Assets evaluated" value={snapshot.daily_summary.assets_evaluated} />
            <SummaryLine label="Research opportunities" value={snapshot.daily_summary.research_opportunities} />
            <SummaryLine label="No-setup decisions" value={snapshot.daily_summary.no_setup_decisions} />
            <SummaryLine label="Stale-data blocks" value={snapshot.daily_summary.stale_data_blocks} />
            <SummaryLine label="Scheduler errors" value={snapshot.daily_summary.scheduler_errors} />
            <SummaryLine label="Simulated orders" value={snapshot.daily_summary.simulated_orders} />
            <SummaryLine label="Open simulated positions" value={snapshot.daily_summary.open_simulated_positions} />
          </div>
        </Card>
      </div>

      <Card title="Scheduler and Data Health" eyebrow="Freshness">
        <div className="dashboardGrid wideLeft">
          <div className="scoreList">
            <span>Scheduler enabled <strong>{snapshot.system_health.scheduler_status === "Disabled" ? "Disabled" : "Enabled"}</strong></span>
            <span>Cadence <strong>{snapshot.system_health.scheduler_cadence ?? "unknown"}</strong></span>
            <span>Last run <strong>{formatDate(snapshot.system_health.last_successful_scheduler_run)}</strong></span>
            <span>Next run <strong>{formatDate(snapshot.system_health.next_scheduled_scan)}</strong></span>
            <span>Latest result <strong>{snapshot.system_health.overall_status}</strong></span>
            <span>Scheduler failures <strong>{snapshot.system_health.scheduler_failures}</strong></span>
            <span>Duplicate candle skips <strong>{snapshot.system_health.duplicate_candle_skips}</strong></span>
          </div>
          <DataTable
            columns={["Asset", "Latest candle", "Expected timeframe", "Freshness", "Detail"]}
            rows={snapshot.assets.slice(0, 10).map((asset) => [asset.symbol, formatDate(asset.latest_candle_timestamp), asset.timeframe, <StatusBadge key="freshness" status={asset.data_freshness} />, asset.data_freshness_detail])}
          />
        </div>
      </Card>

      <Card title="Active Deployments" eyebrow="Simulation only">
        {snapshot.deployments.length ? (
          <DataTable
            columns={["Deployment", "Candidate", "State", "Last candle", "Last decision", "Last scan", "Latest alert", "Position", "Sim PnL", "Links"]}
            rows={snapshot.deployments.map((deployment) => [
              `${deployment.asset} / ${deployment.timeframe} / ${deployment.strategy}`,
              deployment.candidate_identifier,
              deployment.deployment_state,
              formatDate(deployment.last_scanned_candle),
              deployment.last_decision ?? "none",
              formatDate(deployment.last_successful_scan),
              deployment.latest_alert?.alert_type?.replaceAll("_", " ") ?? "none",
              deployment.paper_position ? "open simulated position" : "no simulated position",
              money(deployment.simulated_unrealized_pnl),
              <span key="links" className="inlineLinks"><Link href={deployment.links.run_scan}>Run Scan</Link><Link href={deployment.links.signal_review}>Signal Review</Link><Link href={deployment.links.execution_logs}>Logs</Link></span>
            ])}
          />
        ) : <EmptyState title="No active simulation deployments." body="Deployments created in Paper Lab will appear here when active and simulation-only." action={<Link className="button" href="/paper">Open Paper Lab</Link>} />}
      </Card>

      <div className="dashboardGrid">
        <Card title="Paper Simulation Overview" eyebrow="All values simulated">
          <div className="metricGrid twoCol">
            <MetricCard label="Equity" value={money(snapshot.paper_account.equity)} detail="Simulated" />
            <MetricCard label="Cash" value={money(snapshot.paper_account.cash)} detail="Paper cash" />
            <MetricCard label="Open positions" value={snapshot.paper_account.open_positions} detail="Long-only simulation" />
            <MetricCard label="Realized PnL" value={money(snapshot.paper_account.realized_pnl)} detail="Simulated" />
            <MetricCard label="Unrealized PnL" value={money(snapshot.paper_account.unrealized_pnl)} detail="Simulated" />
          </div>
          <p className="formHint">{snapshot.paper_account.label}</p>
        </Card>
        <Card title="Recent simulated equity movement" eyebrow="Paper account">
          <LineChart values={equityValues.length ? equityValues : [Number(snapshot.paper_account.equity ?? 0)]} label="Simulated paper equity curve" />
        </Card>
      </div>

      <div className="dashboardGrid">
        <Card title="Recent simulated orders" eyebrow="Paper-only">
          {snapshot.paper_account.recent_simulated_orders.length ? (
            <DataTable columns={["Order", "Symbol", "Side", "Type", "Status", "Submitted"]} rows={snapshot.paper_account.recent_simulated_orders.slice(0, 8).map((order) => [order.id, order.symbol, order.side, order.order_type, order.status, formatDate(order.submitted_at)])} />
          ) : <EmptyState title="No simulated orders." body="Paper orders appear only from the internal simulation service." />}
        </Card>
        <Card title="Recent simulated fills" eyebrow="Paper-only">
          {snapshot.paper_account.recent_simulated_fills.length ? (
            <DataTable columns={["Fill", "Symbol", "Side", "Qty", "Price", "Filled"]} rows={snapshot.paper_account.recent_simulated_fills.slice(0, 8).map((fill) => [fill.id, fill.symbol, fill.side, number(fill.quantity), money(fill.fill_price), formatDate(fill.filled_at)])} />
          ) : <EmptyState title="No simulated fills." body="Fills are generated only from stored candle data in the paper simulator." />}
        </Card>
      </div>

      <Card title="Recent Activity Timeline" eyebrow="Audit trail">
        {snapshot.recent_activity.length ? (
          <div className="executionTimeline">
            {snapshot.recent_activity.slice(0, 24).map((item, index) => (
              <article key={`${item.event_type}-${item.timestamp}-${index}`}>
                <span className="eventDot" />
                <div>
                  <strong>{item.event_type.replaceAll("_", " ")}{item.symbol ? ` / ${item.symbol}` : ""}</strong>
                  <p>{item.description}</p>
                  <p>{item.status}</p>
                  <time>{formatDate(item.timestamp)}</time>
                  {item.link ? <Link className="tableLink" href={item.link}>Open related record <ArrowUpRight size={12} /></Link> : null}
                </div>
              </article>
            ))}
          </div>
        ) : <EmptyState title="No recent activity." body="Scheduler scans, alerts, duplicate skips, stale-data blocks, orders, fills, and review actions will appear here." />}
      </Card>
    </div>
  );
}

function DeploymentManagementCenter({ snapshot }: { snapshot: DeploymentManagementSnapshot }) {
  const activeIds = snapshot.deployments.filter((deployment) => deployment.status === "active").map((deployment) => deployment.id);
  const risk = snapshot.portfolio_risk;
  return (
    <>
      <Card title="Deployment Control Center" eyebrow="Multi-asset simulation portfolio">
        <div className="metricGrid">
          <MetricCard label="Deployments" value={snapshot.summary.deployment_count} detail={`${snapshot.summary.active_count} active / ${snapshot.summary.paused_count} paused`} />
          <MetricCard label="Healthy" value={snapshot.summary.healthy_count} tone={Number(snapshot.summary.error_count) ? "error" : Number(snapshot.summary.warning_count) ? "warning" : "success"} detail={`${snapshot.summary.warning_count} warning / ${snapshot.summary.error_count} error`} />
          <MetricCard label="Conflicts" value={snapshot.summary.conflict_count} tone={Number(snapshot.summary.conflict_count) ? "warning" : "success"} />
          <MetricCard label="Gross exposure" value={percent(risk.gross_exposure_pct)} detail="Simulated market value / equity" tone={Number(risk.exposure_limit_breaches) ? "error" : "neutral"} />
          <MetricCard label="Open positions" value={risk.open_positions} detail="Long-only simulation" />
          <MetricCard label="Sim equity" value={money(risk.equity)} detail="Paper portfolio" />
          <MetricCard label="Unrealized PnL" value={money(risk.unrealized_pnl)} detail="Simulated" />
          <MetricCard label="Realized PnL" value={money(risk.realized_pnl)} detail="Simulated" />
        </div>
        <BulkDeploymentControls activeIds={activeIds} />
        <p className="formHint">{snapshot.safety}</p>
      </Card>

      <Card title="Managed Deployments" eyebrow="Control plane">
        {snapshot.deployments.length ? (
          <DataTable
            columns={["Deployment", "Health", "Cadence", "Exposure", "Performance", "Conflicts", "Controls"]}
            rows={snapshot.deployments.map((deployment) => [
              <span key="deployment" className="assetLink">{deployment.symbol} <small>{deployment.timeframe} / {deployment.strategy_name}_{deployment.strategy_version} / {deployment.status}</small></span>,
              <span key="health"><StatusBadge status={deployment.health_status} /> <small>{deployment.health_detail}</small></span>,
              deployment.scan_cadence ?? "scheduler",
              `${percent(deployment.exposure_pct)} / limit ${percent(deployment.max_simulated_exposure_pct ?? 0.1)}`,
              <span key="performance">{money(deployment.performance.unrealized_pnl)} unrealized <small>{deployment.performance.orders} orders / {deployment.performance.fills} fills / last {deployment.performance.last_signal ?? "none"}</small></span>,
              deployment.conflicts.length ? deployment.conflicts.map((conflict) => conflict.type.replaceAll("_", " ")).join(", ") : "none",
              <DeploymentControlPanel key="controls" deployment={deployment} />
            ])}
          />
        ) : <EmptyState title="No simulation deployments yet." body="Create validated candidates in Paper Lab to manage them here." action={<Link className="button" href="/paper">Open Paper Lab</Link>} />}
      </Card>

      <div className="dashboardGrid">
        <Card title="Portfolio-Wide Simulation Risk" eyebrow="Exposure limits">
          <div className="scoreList">
            <SummaryLine label="Paper cash" value={money(risk.cash)} />
            <SummaryLine label="Market value" value={money(risk.market_value)} />
            <SummaryLine label="Gross exposure" value={percent(risk.gross_exposure_pct)} />
            <SummaryLine label="Exposure breaches" value={risk.exposure_limit_breaches} />
            <SummaryLine label="Conflict count" value={risk.conflict_count} />
          </div>
          {risk.top_positions.length ? (
            <DataTable
              columns={["Symbol", "Qty", "Market value", "Unrealized PnL"]}
              rows={risk.top_positions.map((position) => [position.symbol, number(position.quantity), money(position.market_value), money(position.unrealized_pnl)])}
            />
          ) : <EmptyState title="No open simulated positions." body="Risk summary will populate after simulated fills create positions." />}
        </Card>

        <Card title="Conflict Detection" eyebrow="Deployment overlap">
          {snapshot.conflicts.length ? (
            <div className="warningList">
              {snapshot.conflicts.slice(0, 10).map((conflict, index) => <span key={`${conflict.deployment_id}-${conflict.type}-${index}`}><AlertTriangle size={14} /> {conflict.severity}: {conflict.message}</span>)}
            </div>
          ) : <EmptyState title="No deployment conflicts detected." body="No shared asset/timeframe overlap or simulated exposure-limit breach is currently stored." />}
        </Card>
      </div>

      <div className="dashboardGrid">
        <Card title="Asset Comparison" eyebrow="Deployment performance">
          {snapshot.asset_comparison.length ? <ComparisonTable rows={snapshot.asset_comparison} /> : <EmptyState title="No asset comparison yet." body="Deployments will be grouped here by symbol." />}
        </Card>
        <Card title="Strategy Comparison" eyebrow="Deployment performance">
          {snapshot.strategy_comparison.length ? <ComparisonTable rows={snapshot.strategy_comparison} /> : <EmptyState title="No strategy comparison yet." body="Deployments will be grouped here by strategy." />}
        </Card>
      </div>

      <Card title="Deployment Audit History" eyebrow="Recent control and scheduler events">
        {snapshot.audit_history.length ? (
          <div className="executionTimeline compactTimeline">
            {snapshot.audit_history.slice(0, 16).map((item, index) => (
              <article key={`${item.event_type}-${item.created_at}-${index}`}>
                <span className="eventDot" />
                <div>
                  <strong>{item.event_type.replaceAll("_", " ")}</strong>
                  <p>{item.message}</p>
                  <time>{formatDate(item.created_at)}</time>
                </div>
              </article>
            ))}
          </div>
        ) : <EmptyState title="No deployment audit events." body="Pause, resume, control updates, scheduler scans, and bulk actions will appear here." />}
      </Card>
    </>
  );
}

function ComparisonTable({ rows }: { rows: DeploymentManagementSnapshot["asset_comparison"] }) {
  return (
    <DataTable
      columns={["Name", "Deployments", "Health", "Orders/Fills", "Realized", "Unrealized"]}
      rows={rows.map((row) => [
        row.name,
        `${row.active_count} active / ${row.paused_count} paused`,
        `${row.healthy_count} healthy / ${row.warning_count} warning / ${row.error_count} error`,
        `${row.orders} / ${row.fills}`,
        money(row.realized_pnl),
        money(row.unrealized_pnl)
      ])}
    />
  );
}

function StatusBadge({ status }: { status: string }) {
  return <span className={`missionBadge ${statusTone(status)}`}>{status}</span>;
}

function SummaryLine({ label, value }: { label: string; value: unknown }) {
  return <span>{label} <strong>{String(value ?? 0)}</strong></span>;
}

function metric(snapshot: MissionControlSnapshot, key: string) {
  return snapshot.research_summary[key] ?? 0;
}

function formatDate(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "Never";
}

function formatMaybeNumber(value?: string | number | null) {
  if (value === null || value === undefined || value === "") return "n/a";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(4) : String(value);
}

function percent(value?: string | number | null) {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? `${(numeric * 100).toFixed(1)}%` : "0.0%";
}

function tone(status: MissionControlStatus): "neutral" | "success" | "warning" | "error" {
  if (status === "Healthy") return "success";
  if (status === "Error") return "error";
  if (status === "Warning" || status === "Stale") return "warning";
  return "neutral";
}

function statusTone(status: string) {
  if (["Healthy", "Research Opportunity", "Setup Review"].includes(status)) return "success";
  if (["Warning", "Stale", "Stale Data", "Scheduler Error", "Paused"].includes(status)) return "warning";
  if (status === "Error" || status === "Avoid") return "error";
  return "neutral";
}
