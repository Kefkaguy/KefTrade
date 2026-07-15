import Link from "next/link";
import { Activity, CheckCircle2, Clock3, LockKeyhole, ShieldCheck } from "lucide-react";
import {
  AcknowledgeAlertButton,
  CandidateDeploymentScanButton,
  CreatePaperAccount,
  EvidenceNotificationControls,
  PaperOperations,
  PaperSchedulerControls,
  SignalReviewControls,
  TslaPaperScanControls
} from "@/components/PaperActions";
import { Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getPaperSnapshot } from "@/lib/paper";
import { money, number } from "@/lib/format";
import type { StrategyDeployment } from "@/lib/api";

export default async function PaperDashboardPage() {
  const snapshot = await getPaperSnapshot();
  const deployments = snapshot.allDeployments;
  const candidateDeployments = deployments.filter(isCandidateDeployment);
  const candidateDeploymentIds = new Set(candidateDeployments.map((row) => row.id));
  const legacyDeployments = deployments.filter((row) => !candidateDeploymentIds.has(row.id));
  const candidateOrders = snapshot.allOrders.filter((row) => row.deployment_id && candidateDeploymentIds.has(row.deployment_id));
  const candidateFills = snapshot.allFills.filter((row) => row.deployment_id && candidateDeploymentIds.has(row.deployment_id));
  const candidateLogs = snapshot.allLogs.filter((row) => row.deployment_id && candidateDeploymentIds.has(row.deployment_id));
  const candidateAccounts = new Map(snapshot.accountSnapshots.map((row) => [row.account.id, row]));
  const missionControl = snapshot.missionControl;
  const forwardEvidence = recordValue(missionControl, "forward_evidence");
  const readiness = recordValue(missionControl, "readiness");
  const gates = arrayValue(readiness, "gates");
  const closedTradeGate = gates.find((row) => row.name === "forward_closed_trades_minimum");
  const eligibleTrades = numericValue(forwardEvidence, ["eligible_closed_trades", "closed_trades"]);
  const requiredTrades = Number(closedTradeGate?.required_value ?? 20);
  const eligibleExpectancy = nullableNumericValue(forwardEvidence, ["eligible_expectancy", "expectancy"]);
  const eligibleProfitFactor = nullableNumericValue(forwardEvidence, ["eligible_profit_factor", "profit_factor"]);
  const phase10Allowed = Boolean(readiness?.phase_10_allowed);
  const scheduler = snapshot.scheduler;
  const activeCandidateCount = candidateDeployments.filter((row) => row.status === "active").length;
  const awaitingCount = candidateDeployments.filter((row) => row.last_signal === "awaiting_forward_candle").length;
  const unacknowledgedAlerts = snapshot.alerts.filter((row) => !row.acknowledged_at);
  const latestAlert = snapshot.alerts[0] ?? null;
  const legacyTslaDeployment = legacyDeployments.find((row) => row.symbol === "TSLA" && row.timeframe === "1h" && row.status === "active");
  const legacyTslaReview = snapshot.signalReviews.find((row) => row.symbol === "TSLA" && row.timeframe === "1h") ?? null;
  const progress = Math.min(100, requiredTrades > 0 ? (eligibleTrades / requiredTrades) * 100 : 0);

  return (
    <div className="pageStack forwardValidationPage">
      <PageTitle
        title="Candidate Forward Validation"
        description="Independent simulation evidence for Phase 9.12 elite candidates. Phase 10 remains governed by the existing readiness gates."
        actions={<div className="toolbar"><Link className="button ghost" href="/paper/deployments">Deployment controls</Link><Link className="button ghost" href="/paper/orders">All orders</Link></div>}
      />

      <section className="forwardStatusBand">
        <div>
          <span className="eyebrow">PHASE 9.12 / FORWARD EVIDENCE</span>
          <h2>{phase10Allowed ? "Forward gates passed" : "Forward validation in progress"}</h2>
          <p>{phase10Allowed ? "The stored readiness assessment allows the next phase." : "Candidate-linked deployments are collecting untouched prospective evidence."}</p>
        </div>
        <div className={`phaseLockState ${phase10Allowed ? "ready" : "locked"}`}>
          {phase10Allowed ? <CheckCircle2 size={20} /> : <LockKeyhole size={20} />}
          <span>Phase 10</span>
          <strong>{phase10Allowed ? "Eligible" : "Locked"}</strong>
        </div>
      </section>

      <div className="metricGrid">
        <MetricCard label="Elite deployments" value={candidateDeployments.length} detail={`${activeCandidateCount} active / ${candidateDeployments.length - activeCandidateCount} paused`} tone={activeCandidateCount ? "success" : "warning"} />
        <MetricCard label="Eligible closed trades" value={eligibleTrades} detail={`${requiredTrades} required by current readiness gate`} tone={eligibleTrades >= requiredTrades ? "success" : "warning"} />
        <MetricCard label="Forward profit factor" value={formatMetric(eligibleProfitFactor)} detail={eligibleTrades ? "Candidate-linked closed trades" : "Unavailable until eligible closes"} />
        <MetricCard label="Forward expectancy" value={formatMetric(eligibleExpectancy)} detail={eligibleTrades ? "Candidate-linked closed trades" : "Unavailable until eligible closes"} />
      </div>

      <div className="dashboardGrid wideLeft">
        <Card title="Evidence collection" eyebrow="Readiness progress">
          <div className="forwardProgressHeader">
            <div><strong>{eligibleTrades} / {requiredTrades}</strong><span>eligible closed trades</span></div>
            <span>{progress.toFixed(0)}%</span>
          </div>
          <div className="progressTrack forwardProgressTrack" aria-label={`${eligibleTrades} of ${requiredTrades} eligible closed trades`}><i style={{ width: `${progress}%` }} /></div>
          <div className="forwardGateList">
            <GateRow label="Candidate-linked deployments" value={`${candidateDeployments.length} active records`} passed={candidateDeployments.length > 0} />
            <GateRow label="Eligible forward sample" value={`${eligibleTrades} closed trades`} passed={eligibleTrades >= requiredTrades} />
            <GateRow label="Positive expectancy" value={formatMetric(eligibleExpectancy)} passed={eligibleExpectancy !== null && eligibleExpectancy > 0} />
            <GateRow label="Forward profit factor available" value={formatMetric(eligibleProfitFactor)} passed={eligibleProfitFactor !== null} />
          </div>
        </Card>
        <Card title="Collection engine" eyebrow="Scheduler health">
          <div className="forwardSchedulerState">
            <div className={`schedulerPulse ${scheduler?.enabled && !scheduler.latest_error ? "healthy" : "warning"}`}><Activity size={18} /></div>
            <div><strong>{scheduler?.is_running ? "Scan running" : scheduler?.enabled ? "Scheduler active" : "Scheduler stopped"}</strong><span>{scheduler?.cadence ?? "unknown"} cadence</span></div>
          </div>
          <div className="scoreList compactScoreList">
            <span>Last run <strong>{formatDate(scheduler?.last_run_at)}</strong></span>
            <span>Next run <strong>{formatDate(scheduler?.next_run_at)}</strong></span>
            <span>Latest result <strong>{scheduler?.latest_result ?? "No result"}</strong></span>
            <span>Execution errors <strong>{scheduler?.latest_error ?? "None"}</strong></span>
            <span>Awaiting new candle <strong>{awaitingCount} deployments</strong></span>
          </div>
          {scheduler ? <PaperSchedulerControls enabled={scheduler.enabled} cadence={scheduler.cadence} /> : null}
        </Card>
      </div>

      <Card title="Elite candidate deployments" eyebrow="Independent simulation ledgers" action={<span className="statusPill neutral">{candidateDeployments.length} candidates</span>}>
        {candidateDeployments.length ? (
          <div className="forwardCandidateGrid">
            {candidateDeployments.map((deployment) => {
              const accountSnapshot = candidateAccounts.get(deployment.account_id);
              const accountOrders = candidateOrders.filter((row) => row.deployment_id === deployment.id);
              const accountFills = candidateFills.filter((row) => row.deployment_id === deployment.id);
              return (
                <article className="forwardCandidateCard" key={deployment.id}>
                  <header>
                    <div>
                      <span className="candidateId">{deployment.candidate_id}</span>
                      <h3>{deployment.symbol} <small>{deployment.timeframe}</small></h3>
                    </div>
                    <CandidateDeploymentScanButton deploymentId={deployment.id} />
                  </header>
                  <div className="candidateStatusRow">
                    <span className={`statusPill ${deployment.status === "active" ? "success" : "warning"}`}>{deployment.status}</span>
                    <span className="statusPill neutral">{lifecycleLabel(deployment)}</span>
                  </div>
                  <div className="candidateEvidenceStats">
                    <span><strong>{accountOrders.length}</strong>orders</span>
                    <span><strong>{accountFills.length}</strong>fills</span>
                    <span><strong>{money(accountSnapshot?.balances?.realized_pnl ?? 0)}</strong>realized</span>
                  </div>
                  <dl>
                    <div><dt>Strategy</dt><dd>{strategyFamily(deployment)}</dd></div>
                    <div><dt>Forward start</dt><dd>{formatDate(deployment.forward_validation_started_at)}</dd></div>
                    <div><dt>Last scan</dt><dd>{formatDate(deployment.last_scan_at)}</dd></div>
                    <div><dt>Latest state</dt><dd>{signalLabel(deployment.last_signal)}</dd></div>
                  </dl>
                  <p className="candidateCheckResult">{deployment.last_check_result ?? "No forward scan recorded."}</p>
                </article>
              );
            })}
          </div>
        ) : <EmptyState title="No candidate-linked deployments" body="Elite candidate deployments will appear after deterministic promotion and candidate linkage are complete." />}
      </Card>

      <div className="dashboardGrid">
        <Card title="Candidate-linked orders" eyebrow="Forward execution">
          {candidateOrders.length ? <DataTable columns={["Order", "Candidate", "Asset", "Side", "Status"]} rows={candidateOrders.slice(0, 10).map((row) => [row.id, row.candidate_id ?? candidateIdForDeployment(candidateDeployments, row.deployment_id), `${row.symbol} ${row.timeframe}`, row.side, row.status])} /> : <EmptyState title="No candidate-linked orders" body="No Phase 9.12 deployment has produced an eligible forward setup yet." />}
        </Card>
        <Card title="Candidate-linked fills" eyebrow="Forward evidence">
          {candidateFills.length ? <DataTable columns={["Fill", "Candidate", "Asset", "Side", "Price"]} rows={candidateFills.slice(0, 10).map((row) => [row.id, row.candidate_id ?? candidateIdForDeployment(candidateDeployments, row.deployment_id), row.symbol, row.side, money(row.fill_price)])} /> : <EmptyState title="No candidate-linked fills" body="Forward profit factor and expectancy remain unavailable until eligible positions close." />}
        </Card>
      </div>

      <Card title="Forward-validation activity" eyebrow="Candidate audit trail">
        {candidateLogs.length ? (
          <div className="executionTimeline">
            {candidateLogs.slice(0, 18).map((log) => (
              <article key={log.id}><span className="eventDot" /><div><strong>{humanize(log.event_type)}</strong><p>{candidateIdForDeployment(candidateDeployments, log.deployment_id)} / {log.message}</p><time>{formatDate(log.created_at)}</time></div></article>
            ))}
          </div>
        ) : <EmptyState title="No candidate activity" body="Forward scans, simulated orders, fills, and reconciliation events will appear here." />}
      </Card>

      <div className="dashboardGrid">
        <Card title="Evidence alerts" eyebrow="Current system notices">
          <div className="metricGrid twoCol">
            <MetricCard label="Active alerts" value={unacknowledgedAlerts.length} detail="Unacknowledged notices" tone={unacknowledgedAlerts.length ? "warning" : "success"} />
            <MetricCard label="Latest verdict" value={latestAlert?.verdict ?? "No alert"} detail={latestAlert ? humanize(latestAlert.alert_type) : "No stored notice"} />
          </div>
          {latestAlert ? <div className="latestEvidenceAlert"><div><strong>{latestAlert.evidence_summary}</strong><span>{latestAlert.severity} / {formatDate(latestAlert.created_at)}</span></div>{!latestAlert.acknowledged_at ? <AcknowledgeAlertButton alertId={latestAlert.id} /> : <span className="statusPill success">acknowledged</span>}</div> : null}
        </Card>
        <Card title="Simulation guardrails" eyebrow="Execution boundary">
          <div className="guardrailList">
            <span><ShieldCheck size={16} /> Broker connection <strong>Disabled</strong></span>
            <span><ShieldCheck size={16} /> Live routing <strong>Disabled</strong></span>
            <span><ShieldCheck size={16} /> Leverage <strong>Blocked</strong></span>
            <span><Clock3 size={16} /> Evidence origin <strong>Post-deployment only</strong></span>
          </div>
        </Card>
      </div>

      <details className="legacyDisclosure">
        <summary>Legacy simulation tools and account history</summary>
        <div className="legacyContent pageStack">
          <div className="legacyNotice"><strong>Separate legacy workspace</strong><span>These controls and records are excluded from candidate-linked Phase 9.12 forward evidence.</span></div>
          {snapshot.account ? (
            <>
              <div className="dashboardGrid">
                <Card title="Legacy paper account" eyebrow={snapshot.account.name}>
                  <div className="scoreList">
                    <span>Cash <strong>{money(snapshot.balances?.cash_balance)}</strong></span>
                    <span>Equity <strong>{money(snapshot.balances?.equity)}</strong></span>
                    <span>Positions <strong>{snapshot.positions.length}</strong></span>
                    <span>Orders <strong>{snapshot.orders.length}</strong></span>
                  </div>
                  <PaperOperations accountId={snapshot.account.id} />
                </Card>
                <Card title="TSLA compatibility deployment" eyebrow="Legacy simulation">
                  <div className="scoreList">
                    <span>Deployment <strong>{legacyTslaDeployment ? "Active" : "Not active"}</strong></span>
                    <span>Market <strong>TSLA / 1h</strong></span>
                    <span>Evidence eligibility <strong>Excluded from Phase 9.12</strong></span>
                  </div>
                  <TslaPaperScanControls accountId={snapshot.account.id} deploymentId={legacyTslaDeployment?.id} />
                </Card>
              </div>
              <div className="dashboardGrid">
                <Card title="Legacy Signal Review" eyebrow="TSLA review controls"><SignalReviewControls review={legacyTslaReview} deploymentId={legacyTslaDeployment?.id} /></Card>
                <Card title="Legacy notifications" eyebrow="Local browser settings"><EvidenceNotificationControls alerts={snapshot.alerts.filter((row) => row.symbol === "TSLA" || row.symbol === "SYSTEM")} /></Card>
              </div>
              <div className="toolbar"><Link className="button ghost" href="/paper/orders">Legacy orders</Link><Link className="button ghost" href="/paper/positions">Legacy positions</Link><Link className="button ghost" href="/paper/portfolio">Legacy portfolio</Link></div>
            </>
          ) : <CreatePaperAccount />}
        </div>
      </details>
    </div>
  );
}

function isCandidateDeployment(deployment: StrategyDeployment) {
  return Boolean(deployment.campaign_id && deployment.candidate_id && deployment.deployment_origin === "elite_candidate_campaign");
}

function GateRow({ label, value, passed }: { label: string; value: string; passed: boolean }) {
  return <div><span>{passed ? <CheckCircle2 size={15} /> : <Clock3 size={15} />}{label}</span><strong>{value}</strong></div>;
}

function recordValue(source: unknown, key: string): Record<string, any> | null {
  if (!source || typeof source !== "object") return null;
  const value = (source as Record<string, unknown>)[key];
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, any> : null;
}

function arrayValue(source: Record<string, any> | null, key: string): Array<Record<string, any>> {
  const value = source?.[key];
  return Array.isArray(value) ? value : [];
}

function numericValue(source: Record<string, any> | null, keys: string[]) {
  return nullableNumericValue(source, keys) ?? 0;
}

function nullableNumericValue(source: Record<string, any> | null, keys: string[]) {
  for (const key of keys) {
    const value = source?.[key];
    if (value !== null && value !== undefined && Number.isFinite(Number(value))) return Number(value);
  }
  return null;
}

function formatMetric(value: number | null) {
  return value === null ? "Unavailable" : value.toFixed(4);
}

function formatDate(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "Not available";
}

function humanize(value?: string | null) {
  return value ? value.replaceAll("_", " ") : "not available";
}

function lifecycleLabel(deployment: StrategyDeployment) {
  return humanize(deployment.lifecycle_state ?? "forward validation");
}

function signalLabel(value?: string | null) {
  return value === "awaiting_forward_candle" ? "Awaiting forward candle" : humanize(value ?? "not scanned");
}

function strategyFamily(deployment: StrategyDeployment) {
  const family = deployment.parameters?.phase_9_12_strategy_family;
  return typeof family === "string" ? family : `${deployment.strategy_name} ${deployment.strategy_version}`;
}

function candidateIdForDeployment(deployments: StrategyDeployment[], deploymentId?: number | null) {
  return deployments.find((row) => row.id === deploymentId)?.candidate_id ?? "unlinked";
}
