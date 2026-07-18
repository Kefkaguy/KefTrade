export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

import Link from "next/link";
import { Activity, ArrowRight, Check, CheckCircle2, Clock3, LockKeyhole, Radar, ShieldCheck } from "lucide-react";
import { AcknowledgeAlertButton, CandidateDeploymentScanButton, PaperSchedulerControls } from "@/components/PaperActions";
import { DataTable, EmptyState } from "@/components/ResearchUI";
import { getPaperSnapshot } from "@/lib/paper";
import { money } from "@/lib/format";
import type { StrategyDeployment } from "@/lib/api";

export default async function PaperDashboardPage() {
  const snapshot = await getPaperSnapshot();
  const candidateDeployments = snapshot.allDeployments.filter(isCandidateDeployment);
  const deploymentIds = new Set(candidateDeployments.map((row) => row.id));
  const candidateOrders = snapshot.allOrders.filter((row) => row.deployment_id && deploymentIds.has(row.deployment_id));
  const candidateFills = snapshot.allFills.filter((row) => row.deployment_id && deploymentIds.has(row.deployment_id));
  const candidateLogs = snapshot.allLogs.filter((row) => row.deployment_id && deploymentIds.has(row.deployment_id));
  const candidateAccounts = new Map(snapshot.accountSnapshots.map((row) => [row.account.id, row]));
  const forwardEvidence = recordValue(snapshot.missionControl, "forward_evidence");
  const readiness = recordValue(snapshot.missionControl, "readiness");
  const closedTradeGate = arrayValue(readiness, "gates").find((row) => row.name === "forward_closed_trades_minimum");
  const eligibleTrades = numericValue(forwardEvidence, ["eligible_closed_trades", "closed_trades"]);
  const requiredTrades = Number(closedTradeGate?.required_value ?? 20);
  const expectancy = nullableNumericValue(forwardEvidence, ["eligible_expectancy", "expectancy"]);
  const profitFactor = nullableNumericValue(forwardEvidence, ["eligible_profit_factor", "profit_factor"]);
  const phase10Allowed = Boolean(readiness?.phase_10_allowed);
  const scheduler = snapshot.scheduler;
  const awaitingCount = candidateDeployments.filter((row) => row.last_signal === "awaiting_forward_candle").length;
  const activeCount = candidateDeployments.filter((row) => row.status === "active").length;
  const alerts = snapshot.alerts.filter((row) => !row.acknowledged_at);
  const latestAlert = snapshot.alerts[0] ?? null;
  const progress = Math.min(100, requiredTrades ? (eligibleTrades / requiredTrades) * 100 : 0);

  return (
    <div className="forwardWorkspacePage">
      <header className="forwardHero">
        <div>
          <span className="eyebrow">Prospective evidence · Phase 9.12</span>
          <h1>Forward validation</h1>
          <p>Observe elite candidates in untouched market data before any strategy can advance.</p>
          <div className="forwardHeroActions">
            <Link className="button secondary" href="/paper/deployments">Manage deployments <ArrowRight size={15} /></Link>
            <span><ShieldCheck size={15} /> Simulation only</span>
          </div>
        </div>
        <div className={`forwardPhaseSeal ${phase10Allowed ? "ready" : "locked"}`}>
          {phase10Allowed ? <CheckCircle2 size={26} /> : <LockKeyhole size={26} />}
          <span>Phase 10</span>
          <strong>{phase10Allowed ? "Eligible" : "Locked"}</strong>
          <small>{phase10Allowed ? "Readiness gates passed" : "Awaiting independent evidence"}</small>
        </div>
      </header>

      <div className="forwardWorkspaceGrid">
        <main className="forwardEvidenceMain">
          <section className="forwardReadiness">
            <header>
              <div><span className="sectionLabel">Evidence collection</span><h2>{phase10Allowed ? "Readiness established" : "Building the forward sample"}</h2></div>
              <strong>{progress.toFixed(0)}%</strong>
            </header>
            <div className="forwardSampleCount"><strong>{eligibleTrades}</strong><span>of {requiredTrades} eligible closed trades</span></div>
            <div className="forwardEvidenceTrack"><i style={{ width: `${progress}%` }} /></div>
            <div className="forwardGatePath">
              <EvidenceGate index="01" label="Deploy elite candidates" value={`${candidateDeployments.length} linked`} passed={candidateDeployments.length > 0} />
              <EvidenceGate index="02" label="Collect independent trades" value={`${eligibleTrades} closed`} passed={eligibleTrades >= requiredTrades} />
              <EvidenceGate index="03" label="Confirm positive expectancy" value={formatMetric(expectancy)} passed={expectancy !== null && expectancy > 0} />
              <EvidenceGate index="04" label="Measure profit factor" value={formatMetric(profitFactor)} passed={profitFactor !== null} />
            </div>
          </section>

          <section className="forwardCandidatesSection">
            <header><div><span className="sectionLabel">Independent ledgers</span><h2>Elite candidate deployments</h2></div><span>{candidateDeployments.length} candidates</span></header>
            {candidateDeployments.length ? (
              <div className="forwardCandidateGrid">
                {candidateDeployments.map((deployment) => {
                  const account = candidateAccounts.get(deployment.account_id);
                  const orders = candidateOrders.filter((row) => row.deployment_id === deployment.id);
                  const fills = candidateFills.filter((row) => row.deployment_id === deployment.id);
                  return <article className="forwardCandidateCard" key={deployment.id}>
                    <header><div><span>{deployment.candidate_id}</span><h3>{deployment.symbol} <small>{deployment.timeframe}</small></h3></div><CandidateDeploymentScanButton deploymentId={deployment.id} /></header>
                    <div className="forwardCandidateState"><i className={deployment.status === "active" ? "active" : ""} />{deployment.status}<span>{lifecycleLabel(deployment)}</span></div>
                    <div className="forwardCandidateStats"><span><strong>{orders.length}</strong> Orders</span><span><strong>{fills.length}</strong> Fills</span><span><strong>{money(account?.balances?.realized_pnl ?? 0)}</strong> Realized</span></div>
                    <dl><div><dt>Strategy</dt><dd>{strategyFamily(deployment)}</dd></div><div><dt>Forward start</dt><dd>{formatDate(deployment.forward_validation_started_at)}</dd></div><div><dt>Last scan</dt><dd>{formatDate(deployment.last_scan_at)}</dd></div><div><dt>Latest state</dt><dd>{signalLabel(deployment.last_signal)}</dd></div></dl>
                    <p>{deployment.last_check_result ?? "No forward scan recorded."}</p>
                  </article>;
                })}
              </div>
            ) : <div className="forwardEmptyState"><Radar size={24} /><EmptyState title="No candidates are collecting forward evidence" body="Elite deployments will appear here after deterministic promotion and candidate linkage." action={<Link className="button secondary" href="/research-intelligence">Review research candidates</Link>} /></div>}
          </section>

          <details className="forwardLedger" open={candidateOrders.length > 0 || candidateFills.length > 0}>
            <summary><div><span className="sectionLabel">Execution ledger</span><strong>Orders and fills</strong></div><span>{candidateOrders.length} orders · {candidateFills.length} fills</span><ArrowRight size={15} /></summary>
            <div className="forwardLedgerGrid">
              <section><h3>Candidate-linked orders</h3>{candidateOrders.length ? <DataTable columns={["Order", "Candidate", "Asset", "Side", "Status"]} rows={candidateOrders.slice(0, 10).map((row) => [row.id, row.candidate_id ?? candidateIdForDeployment(candidateDeployments, row.deployment_id), `${row.symbol} ${row.timeframe}`, row.side, row.status])} /> : <EmptyState title="No eligible orders" body="No candidate has produced an eligible forward setup." />}</section>
              <section><h3>Candidate-linked fills</h3>{candidateFills.length ? <DataTable columns={["Fill", "Candidate", "Asset", "Side", "Price"]} rows={candidateFills.slice(0, 10).map((row) => [row.id, row.candidate_id ?? candidateIdForDeployment(candidateDeployments, row.deployment_id), row.symbol, row.side, money(row.fill_price)])} /> : <EmptyState title="No forward fills" body="Forward metrics remain unavailable until eligible positions close." />}</section>
            </div>
          </details>

          <section className="forwardActivity">
            <header><span className="sectionLabel">Candidate audit trail</span><h2>Forward-validation activity</h2></header>
            {candidateLogs.length ? <div className="executionTimeline">{candidateLogs.slice(0, 18).map((log) => <article key={log.id}><span className="eventDot" /><div><strong>{humanize(log.event_type)}</strong><p>{candidateIdForDeployment(candidateDeployments, log.deployment_id)} / {log.message}</p><time>{formatDate(log.created_at)}</time></div></article>)}</div> : <EmptyState title="No candidate activity yet" body="Forward scans, simulated orders, fills, and reconciliation events will form this audit trail." />}
          </section>
        </main>

        <aside className="forwardOperationsRail">
          <section className="forwardEngineStatus">
            <header><span className={`schedulerPulse ${scheduler?.enabled && !scheduler.latest_error ? "healthy" : "warning"}`}><Activity size={16} /></span><div><strong>{scheduler?.is_running ? "Scan running" : scheduler?.enabled ? "Collection engine active" : "Collection engine stopped"}</strong><small>{scheduler?.cadence ?? "unknown"} cadence</small></div></header>
            <dl><RailRow label="Last run" value={formatDate(scheduler?.last_run_at)} /><RailRow label="Next run" value={formatDate(scheduler?.next_run_at)} /><RailRow label="Awaiting candle" value={`${awaitingCount} deployments`} /><RailRow label="Latest result" value={scheduler?.latest_result ?? "No result"} /><RailRow label="Errors" value={scheduler?.latest_error ?? "None"} /></dl>
            {scheduler ? <PaperSchedulerControls enabled={scheduler.enabled} cadence={scheduler.cadence} /> : null}
          </section>
          <section className="forwardSnapshot"><span className="sectionLabel">Current sample</span><div><strong>{activeCount}</strong><span>active deployments</span></div><div><strong>{eligibleTrades}</strong><span>eligible closes</span></div><div><strong>{formatMetric(profitFactor)}</strong><span>profit factor</span></div><div><strong>{formatMetric(expectancy)}</strong><span>expectancy</span></div></section>
          <section className="forwardNotices"><header><span className="sectionLabel">Evidence notices</span><strong>{alerts.length} active</strong></header>{latestAlert ? <><p>{latestAlert.evidence_summary}</p><small>{latestAlert.severity} · {formatDate(latestAlert.created_at)}</small>{!latestAlert.acknowledged_at ? <AcknowledgeAlertButton alertId={latestAlert.id} /> : <span className="statusPill success">Acknowledged</span>}</> : <div className="forwardAllClear"><Check size={15} /> No stored notice</div>}</section>
          <section className="forwardGuardrails"><span className="sectionLabel">Execution boundary</span><p><ShieldCheck size={15} /> Broker connection <strong>Disabled</strong></p><p><ShieldCheck size={15} /> Live routing <strong>Disabled</strong></p><p><ShieldCheck size={15} /> Leverage <strong>Blocked</strong></p><p><Clock3 size={15} /> Evidence <strong>Post-deployment</strong></p></section>
        </aside>
      </div>
    </div>
  );
}

function EvidenceGate({ index, label, value, passed }: { index: string; label: string; value: string; passed: boolean }) { return <div className={passed ? "passed" : "pending"}><span>{passed ? <Check size={14} /> : index}</span><div><strong>{label}</strong><small>{value}</small></div></div>; }
function RailRow({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd>{value}</dd></div>; }
function isCandidateDeployment(deployment: StrategyDeployment) { return Boolean(deployment.campaign_id && deployment.candidate_id && deployment.deployment_origin === "elite_candidate_campaign"); }
function recordValue(source: unknown, key: string): Record<string, any> | null { if (!source || typeof source !== "object") return null; const value = (source as Record<string, unknown>)[key]; return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, any> : null; }
function arrayValue(source: Record<string, any> | null, key: string): Array<Record<string, any>> { return Array.isArray(source?.[key]) ? source[key] : []; }
function numericValue(source: Record<string, any> | null, keys: string[]) { return nullableNumericValue(source, keys) ?? 0; }
function nullableNumericValue(source: Record<string, any> | null, keys: string[]) { for (const key of keys) { const value = source?.[key]; if (value !== null && value !== undefined && Number.isFinite(Number(value))) return Number(value); } return null; }
function formatMetric(value: number | null) { return value === null ? "Pending" : value.toFixed(3); }
function formatDate(value?: string | null) { return value ? new Date(value).toLocaleString() : "Not available"; }
function humanize(value?: string | null) { return value ? value.replaceAll("_", " ") : "not available"; }
function lifecycleLabel(deployment: StrategyDeployment) { return humanize(deployment.lifecycle_state ?? "forward validation"); }
function signalLabel(value?: string | null) { return value === "awaiting_forward_candle" ? "Awaiting forward candle" : humanize(value ?? "not scanned"); }
function strategyFamily(deployment: StrategyDeployment) { const family = deployment.parameters?.phase_9_12_strategy_family; return typeof family === "string" ? family : `${deployment.strategy_name} ${deployment.strategy_version}`; }
function candidateIdForDeployment(deployments: StrategyDeployment[], deploymentId?: number | null) { return deployments.find((row) => row.id === deploymentId)?.candidate_id ?? "unlinked"; }
