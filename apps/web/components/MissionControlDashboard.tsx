"use client";

import Link from "next/link";
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  Bot,
  BriefcaseBusiness,
  CheckCircle2,
  CircleGauge,
  FlaskConical,
  LineChart as LineChartIcon,
  ListChecks,
  RadioTower,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  WalletCards,
  XCircle,
  Zap
} from "lucide-react";
import { BulkDeploymentControls, DeploymentControlPanel } from "@/components/DeploymentManagementActions";
import { Card, DataTable, EmptyState, LineChart, MetricCard, PageTitle } from "@/components/ResearchUI";
import { useInterfaceMode } from "@/components/InterfaceModeContext";
import type { DeploymentManagementSnapshot, MissionControlSnapshot, MissionControlStatus } from "@/lib/api";
import { money, number } from "@/lib/format";
import { useEffect, useState } from "react";

type DashboardProps = {
  snapshot: MissionControlSnapshot;
  deploymentManagement: DeploymentManagementSnapshot | null;
  deploymentError?: string | null;
  researchIntelligence: any | null;
};

type SimpleFailureReason = {
  label: string;
  count: number;
  detail: string;
};

export function MissionControlDashboard({ snapshot, deploymentManagement, deploymentError, researchIntelligence }: DashboardProps) {
  const { mode } = useInterfaceMode();
  const equityValues = snapshot.paper_account.recent_equity_curve.map((row) => Number(row.equity)).filter(Number.isFinite);
  const highestPriority = snapshot.review_queue[0];
  const bestCandidate = researchIntelligence?.rankings?.[0];
  const readiness = authoritativeReadiness(snapshot);
  const campaign = authoritativeCampaign(snapshot);
  const forwardEvidence = authoritativeForwardEvidence(snapshot);
  const activeDiagnostics = activeSubsystemDiagnostics(snapshot);
  const criticalCount = activeDiagnostics.length + Number(snapshot.system_health.scheduler_status === "Error") + Number(metricNumber(snapshot, "scheduler_failures") > 0);
  const readinessState = readiness.state;
  const readinessScore = Number(readiness.score ?? 0);
  const researchOpportunityCount = metricNumber(snapshot, "research_opportunities");
  const activeAlerts = snapshot.system_health.unacknowledged_alert_count ?? 0;
  const chartDistributions = buildDashboardDistributions(snapshot, researchIntelligence);

  if (mode === "simple") {
    return <SimpleMissionControl snapshot={snapshot} deploymentManagement={deploymentManagement} deploymentError={deploymentError} researchIntelligence={researchIntelligence} />;
  }

  return (
    <div className="pageStack missionControl commandCenter">
      <PageTitle
        title="KefTrade Mission Control"
        description="Research health, priorities, readiness, and paper performance."
        actions={<QuickActions />}
      />

      <section className={`readinessBanner ${readinessTone(readinessState)}`}>
        <div>
          <span className="eyebrow">Phase 10 Readiness</span>
          <h2>{readinessState === "ready_for_phase_10" ? "READY" : readinessState === "unknown" ? "UNAVAILABLE" : "NOT READY"}</h2>
        </div>
        <div className="readinessFacts">
          <SummaryLine label="Score" value={readiness.score ?? "unavailable"} />
          <SummaryLine label="Blocking gates" value={readiness.blocking_gate_count} />
          <SummaryLine label="Last updated" value={formatDate(snapshot.generated_at)} />
          <SummaryLine label="Data age" value={dataAge(snapshot.generated_at)} />
          <SummaryLine label="Snapshot" value={snapshot.snapshot_version ?? "legacy"} />
        </div>
      </section>

      <section className={`executiveHero ${semanticTone(snapshot.system_health.overall_status)}`}>
        <div>
          <span className="eyebrow">EVIDENCE OS STATUS</span>
          <h2>{executiveHeadline(snapshot)}</h2>
          <div className="heroStatusRail">
            <StatusDot tone={tone(snapshot.system_health.research_engine_status)} label="Engine" value={snapshot.system_health.research_engine_status} />
            <StatusDot tone={readinessTone(readinessState)} label="Phase 10" value={readinessState.replaceAll("_", " ")} />
            <StatusDot tone={highestPriority ? "warning" : "success"} label="Next" value={highestPriority ? highestPriority.symbol : "Clear"} />
          </div>
        </div>
        <div className="heroDecisionPanel">
          <StatusPill label="System" value={snapshot.system_health.overall_status} />
          <StatusPill label="Readiness" value={readinessState.replaceAll("_", " ")} />
          <StatusPill label="Safety" value={snapshot.safety.live_routing_enabled ? "Critical" : "Protected"} />
        </div>
      </section>

      <nav className="commandNav" aria-label="Mission Control sections">
        {[
          ["System Health", "#system-health", RadioTower],
          ["Research Intelligence", "#research-intelligence", Sparkles],
          ["Paper Trading", "#paper-trading", WalletCards],
          ["Deployments", "#deployments", BriefcaseBusiness],
          ["Campaigns", "#campaigns", FlaskConical],
          ["Market Coverage", "#market-coverage", CircleGauge],
          ["Activity", "#activity", Activity]
        ].map(([label, href, Icon]) => (
          <Link key={String(href)} href={String(href)}><Icon size={15} /> {String(label)}</Link>
        ))}
      </nav>

      <section className="executiveKpiGrid" aria-label="Executive overview">
        <ExecutiveKpi icon={<RadioTower size={18} />} label="Research Engine" value={snapshot.system_health.research_engine_status} detail="Engine" tone={tone(snapshot.system_health.research_engine_status)} />
        <ExecutiveKpi icon={<ShieldCheck size={18} />} label="Phase 10 Readiness" value={readinessLabel(readinessState, readinessScore)} detail={`${readiness.blocking_gate_count} blockers`} tone={readinessTone(readinessState)} />
        <ExecutiveKpi icon={<BriefcaseBusiness size={18} />} label="Active Deployments" value={snapshot.system_health.active_deployment_count} detail="Live sims" tone="info" />
        <ExecutiveKpi icon={<Sparkles size={18} />} label="Research Opportunities" value={researchOpportunityCount} detail="Open" tone={researchOpportunityCount ? "success" : "neutral"} />
        <ExecutiveKpi icon={<WalletCards size={18} />} label="Paper Equity" value={money(snapshot.paper_account.equity)} detail={money(snapshot.paper_account.unrealized_pnl)} tone={Number(snapshot.paper_account.unrealized_pnl) >= 0 ? "success" : "warning"} />
        <ExecutiveKpi icon={<Zap size={18} />} label="Today's Scans" value={String(snapshot.daily_summary.scans_completed ?? 0)} detail={`${snapshot.daily_summary.assets_evaluated ?? 0} assets`} tone="info" />
        <ExecutiveKpi icon={<AlertTriangle size={18} />} label="Active Alerts" value={activeAlerts} detail={`${snapshot.review_queue.length} queued`} tone={activeAlerts ? "warning" : "success"} />
        <ExecutiveKpi icon={<ListChecks size={18} />} label="Scheduler Health" value={snapshot.system_health.scheduler_status} detail={snapshot.system_health.scheduler_cadence ?? "Cadence n/a"} tone={tone(snapshot.system_health.scheduler_status)} />
      </section>

      <section className="priorityStrip" aria-label="Immediate priorities">
        <PriorityCard icon={<AlertTriangle size={18} />} label="Highest Priority" value={highestPriority ? `${highestPriority.symbol} / ${highestPriority.reason}` : "Clear"} detail={highestPriority?.current_verdict ?? "No urgent review"} tone={highestPriority ? "warning" : "success"} href={highestPriority?.action.href ?? "/mission-control"} />
        <PriorityCard icon={<CheckCircle2 size={18} />} label="Health" value={criticalCount ? "Review" : "Normal"} detail={`${snapshot.subsystem_errors.length} warnings`} tone={criticalCount ? "warning" : "success"} href="#system-health" />
        <PriorityCard icon={<TrendingUp size={18} />} label="Best Candidate" value={bestCandidate ? String(bestCandidate.candidate_id ?? "Ranked") : "None"} detail={bestCandidate ? `${bestCandidate.symbol ?? "n/a"} / ${formatMaybeNumber(bestCandidate.research_score)}` : "Run validation"} tone={bestCandidate ? "info" : "neutral"} href="/research-intelligence" />
        <PriorityCard icon={<XCircle size={18} />} label="Blocked" value={String(snapshot.research_campaigns?.blocked_data_jobs ?? metricNumber(snapshot, "stale_data_blocks"))} detail="Data blocks" tone={Number(snapshot.research_campaigns?.blocked_data_jobs ?? 0) || metricNumber(snapshot, "stale_data_blocks") ? "warning" : "success"} href="#campaigns" />
      </section>

      {activeDiagnostics.length ? (
        <Card title="Attention Required" eyebrow="Subsystem warnings">
          <DiagnosticRows rows={activeDiagnostics.map((error: any) => ({
            source: error.source ?? error.subsystem,
            severity: error.severity ?? "warning",
            detail: error.error,
            recommended_fix: error.recommended_fix,
            timestamp: error.timestamp
          }))} empty="No subsystem warnings." />
        </Card>
      ) : null}

      <section className="sectionStack" id="system-health">
        <SectionHeader eyebrow="System Health" title="State and readiness" />
        <div className="dashboardGrid wideLeft">
          <Card title="Readiness Progress" eyebrow="Phase 10 gates">
            <div className="readinessLayout">
              <CircularScore value={readinessScore} label={readinessState.replaceAll("_", " ")} />
              <div className="scoreList">
                <SummaryLine label="Integrity audit" value={String(snapshot.production_validation?.data_integrity_status ?? "unknown")} />
                <SummaryLine label="Safety audit" value={String(snapshot.production_validation?.safety_audit_status ?? "unknown")} />
                <SummaryLine label="Blocking gates" value={readiness.blocking_gate_count} />
                <SummaryLine label="Last assessment" value={formatDate(readiness.last_assessed_at)} />
              </div>
            </div>
          </Card>
          <Card title="System Signals" eyebrow="Current state">
            <div className="signalGrid">
              <SignalItem label="Research engine" value={snapshot.system_health.research_engine_status} tone={tone(snapshot.system_health.research_engine_status)} />
              <SignalItem label="Scheduler" value={snapshot.system_health.scheduler_status} tone={tone(snapshot.system_health.scheduler_status)} />
              <SignalItem label="Data freshness" value={snapshot.system_health.overall_data_freshness} tone={tone(snapshot.system_health.overall_data_freshness)} />
              <SignalItem label="Safety" value="Simulation protected" tone="success" />
            </div>
          </Card>
        </div>
        <div className="dashboardGrid">
          <Card title="Readiness Gates" eyebrow="Blockers">
            <GateRows gates={readiness.gates} />
          </Card>
          <Card title="Health Checks" eyebrow="Automated verification">
            <DiagnosticRows rows={healthDiagnostics(snapshot.health?.checks ?? snapshot.production_validation?.health_checks)} empty="All engineering health checks passed." />
          </Card>
        </div>
      </section>

      <section className="sectionStack" id="research-intelligence">
        <SectionHeader eyebrow="Research Intelligence" title="What matters now" />
        <div className="dashboardGrid wideLeft">
          <Card title="Opportunity Command" eyebrow="Stored evidence ranking" action={<Link className="tableLink" href="/research-intelligence">Open full dashboard <ArrowUpRight size={12} /></Link>}>
            {researchIntelligence ? (
              <div className="opportunityStack">
                <FeaturedCandidate row={bestCandidate} />
                <div className="miniChartGrid">
                  <CompactBars title="Candidate Status" rows={chartDistributions.candidateStatus} />
                  <CompactBars title="Strategy Distribution" rows={chartDistributions.strategyDistribution} />
                </div>
              </div>
            ) : <EmptyState title="Research Intelligence unavailable." body="Rankings have not loaded." />}
          </Card>
          <Card title="Research Learning" eyebrow="Adaptive strategy improvement">
            {snapshot.research_learning ? (
              <div className="priorityList">
                <PriorityRow label="Current priority" value={String(snapshot.research_learning.current_priorities?.[0] ?? "none")} tone="info" />
                <PriorityRow label="Strongest idea" value={String(snapshot.research_learning.strongest_emerging_ideas?.[0] ?? "none")} tone="success" />
                <PriorityRow label="Recurring failure" value={String(snapshot.research_learning.recurring_failures?.[0] ?? "none")} tone="warning" />
                <PriorityRow label="Recurring success" value={String(snapshot.research_learning.recurring_successes?.[0] ?? "none")} tone="success" />
                <PriorityRow label="Recommendation queue" value={String(snapshot.research_learning.recommendation_queue?.length ?? 0)} tone="info" />
              </div>
            ) : <EmptyState title="Research learning unavailable." body="No learning summary yet." />}
          </Card>
        </div>
      </section>

      <section className="sectionStack" id="paper-trading">
        <SectionHeader eyebrow="Paper Trading" title="Portfolio snapshot" />
        <div className="dashboardGrid wideLeft">
          <Card title="Paper Equity Curve" eyebrow="Simulation only">
            <LineChart values={equityValues.length ? equityValues : [Number(snapshot.paper_account.equity ?? 0)]} label="Simulated paper equity curve" />
          </Card>
          <Card title="Paper Portfolio" eyebrow="All values simulated">
            <div className="metricGrid twoCol compactMetrics">
              <MetricCard label="Equity" value={money(snapshot.paper_account.equity)} />
              <MetricCard label="Cash" value={money(snapshot.paper_account.cash)} />
              <MetricCard label="Open positions" value={snapshot.paper_account.open_positions} />
              <MetricCard label="Realized PnL" value={money(snapshot.paper_account.realized_pnl)} />
              <MetricCard label="Unrealized PnL" value={money(snapshot.paper_account.unrealized_pnl)} tone={Number(snapshot.paper_account.unrealized_pnl) >= 0 ? "success" : "warning"} />
            </div>
            <p className="formHint">{snapshot.paper_account.label}</p>
          </Card>
        </div>
      </section>

      <section className="sectionStack" id="deployments">
        <SectionHeader eyebrow="Active Deployments" title="Simulation control" />
        {deploymentManagement ? <DeploymentManagementCenter snapshot={deploymentManagement} compact /> : (
          <Card title="Deployment Control Center unavailable" eyebrow="Subsystem warning">
            <EmptyState title="Unable to load deployment management." body={deploymentError ?? "Unknown deployment management error."} />
          </Card>
        )}
      </section>

      <section className="sectionStack" id="campaigns">
        <SectionHeader eyebrow="Campaign Operations" title="Research execution" />
        <div className="dashboardGrid wideLeft">
          <Card title="Campaign Throughput" eyebrow="Worker scheduler">
            <div className="opsSummary">
              <MetricCard label="Queue depth" value={campaign.queue_depth ?? "unavailable"} />
              <MetricCard label="Running jobs" value={campaign.running_jobs ?? "unavailable"} />
              <MetricCard label="Completed 24h" value={campaign.completed_last_24h ?? "unavailable"} />
              <MetricCard label="Failure rate" value={percent(snapshot.production_validation?.failure_rate ?? 0)} tone={Number(snapshot.production_validation?.failure_rate ?? 0) ? "warning" : "success"} />
            </div>
            <CompactBars title="Research Progress" rows={chartDistributions.campaignProgress} />
          </Card>
          <Card title="Production Validation" eyebrow="Phase 10 readiness proof">
            <div className="priorityList">
              <PriorityRow label="Campaign" value={String(campaign.name ?? campaign.state ?? "Campaign state unavailable")} tone="info" />
              <PriorityRow label="Campaign ID" value={String(campaign.id ?? "unavailable")} tone="neutral" />
              <PriorityRow label="State" value={String(campaign.state ?? "unavailable")} tone={campaign.state === "running" ? "info" : "warning"} />
              <PriorityRow label="Duration" value={String(snapshot.production_validation?.validation_duration ?? "not established")} tone="neutral" />
              <PriorityRow label="Worker uptime" value={`${snapshot.workers?.active ?? snapshot.production_validation?.worker_uptime?.active_workers ?? 0} workers / ${snapshot.production_validation?.worker_uptime?.max_hours ?? 0}h max`} tone="info" />
              <PriorityRow label="Eligible closed trades" value={String(forwardEvidence.eligible_closed_trades ?? forwardEvidence.closed_trades ?? "unavailable")} tone={Number(forwardEvidence.eligible_closed_trades ?? forwardEvidence.closed_trades ?? 0) ? "info" : "warning"} />
              <PriorityRow label="Excluded closed trades" value={String(forwardEvidence.excluded_closed_trades ?? 0)} tone={Number(forwardEvidence.excluded_closed_trades ?? 0) ? "warning" : "success"} />
              <PriorityRow label="All simulation trades" value={String(forwardEvidence.all_simulation_closed_trades ?? "unavailable")} tone="neutral" />
              <PriorityRow label="Eligible expectancy" value={formatMaybeNumber(forwardEvidence.eligible_expectancy ?? forwardEvidence.expectancy)} tone={Number(forwardEvidence.eligible_expectancy ?? forwardEvidence.expectancy ?? 0) > 0 ? "success" : "warning"} />
              <PriorityRow label="All-simulation expectancy" value={formatMaybeNumber(forwardEvidence.all_simulation_expectancy)} tone="neutral" />
              <PriorityRow label="Retry rate" value={percent(snapshot.production_validation?.retry_rate ?? 0)} tone={Number(snapshot.production_validation?.retry_rate ?? 0) ? "warning" : "success"} />
              <PriorityRow label="Data-block rate" value={percent(snapshot.production_validation?.data_block_rate ?? 0)} tone={Number(snapshot.production_validation?.data_block_rate ?? 0) ? "warning" : "success"} />
            </div>
            <div className="miniChartGrid validationCharts">
              <CompactBars title="Gate Progress" rows={gateProgressRows(readiness.gates)} />
              <CompactBars title="Forward Evidence" rows={forwardEvidenceRows(forwardEvidence)} />
            </div>
          </Card>
        </div>
      </section>

      <section className="sectionStack" id="market-coverage">
        <SectionHeader eyebrow="Market Coverage" title="Coverage and alerts" />
        <div className="dashboardGrid">
          <Card title="Asset Distribution" eyebrow="Monitored universe">
            <CompactBars title="Asset Classes" rows={chartDistributions.assetDistribution} />
          </Card>
          <Card title="Alert Trends" eyebrow="Current alert mix">
            <CompactBars title="Queue and alerts" rows={chartDistributions.alertTrend} />
          </Card>
        </div>
      </section>

      <section className="sectionStack" id="activity">
        <SectionHeader eyebrow="Recent Activity" title="Latest events" />
        <Card title="Recent Activity Timeline" eyebrow="Audit trail">
          {snapshot.recent_activity.length ? (
            <div className="executionTimeline executiveTimeline">
              {snapshot.recent_activity.slice(0, 12).map((item, index) => (
                <article key={`${item.event_type}-${item.timestamp}-${index}`}>
                  <span className="eventDot" />
                  <div>
                    <strong>{item.event_type.replaceAll("_", " ")}{item.symbol ? ` / ${item.symbol}` : ""}</strong>
                    <p>{item.description}</p>
                    <time>{formatDate(item.timestamp)}</time>
                    {item.link ? <Link className="tableLink" href={item.link}>Open <ArrowUpRight size={12} /></Link> : null}
                  </div>
                </article>
              ))}
            </div>
          ) : <EmptyState title="No recent activity." body="No events yet." />}
        </Card>
      </section>

      <section className="sectionStack">
        <SectionHeader eyebrow="Details" title="Drill down" />
        <div className="detailAccordion">
          <DetailsPanel title="Deployment controls and comparisons" defaultOpen>
            {deploymentManagement ? <DeploymentManagementCenter snapshot={deploymentManagement} /> : <EmptyState title="Deployment details unavailable." body={deploymentError ?? "Unknown deployment management error."} />}
          </DetailsPanel>
          <DetailsPanel title="Research review queue and daily summary">
            <ResearchQueueAndDailySummary snapshot={snapshot} />
          </DetailsPanel>
          <DetailsPanel title="Scheduler and data health">
            <SchedulerAndDataHealth snapshot={snapshot} />
          </DetailsPanel>
          <DetailsPanel title="Multi-asset research table">
            <MultiAssetResearchTable snapshot={snapshot} />
          </DetailsPanel>
          <DetailsPanel title="Campaign worker scheduler details">
            <CampaignWorkerDetails snapshot={snapshot} />
          </DetailsPanel>
          <DetailsPanel title="Paper orders and fills">
            <PaperLedgerDetails snapshot={snapshot} />
          </DetailsPanel>
          <DetailsPanel title="Full activity timeline">
            <FullActivityTimeline snapshot={snapshot} />
          </DetailsPanel>
        </div>
      </section>
    </div>
  );
}

function SimpleMissionControl({ snapshot, deploymentManagement, deploymentError, researchIntelligence }: DashboardProps) {
  const latest = authoritativeLatestCompletedCampaign(snapshot);
  const campaignOptions = completedCampaignOptions(snapshot, latest);
  const [selectedCampaignId, setSelectedCampaignId] = useState<string>(String(latest?.id ?? ""));
  useEffect(() => {
    if (!selectedCampaignId && latest?.id) {
      setSelectedCampaignId(String(latest.id));
    }
  }, [latest?.id, selectedCampaignId]);
  const campaign = selectedCampaignId === String(latest?.id ?? "") ? latest : campaignOptions.find((row) => String(row.id) === selectedCampaignId) ?? latest;
  const bestCandidate = campaign?.best_candidate ?? researchIntelligence?.rankings?.[0] ?? snapshot.review_queue[0] ?? null;
  const generated = Number(campaign?.generated_candidates ?? campaign?.requested_candidates ?? 0);
  const tested = Number(campaign?.tested_candidates ?? generated);
  const jobsExecuted = Number(campaign?.jobs_executed ?? campaign?.completed_jobs ?? 0);
  const evidenceRejectedJobs = Number(campaign?.jobs_rejected_by_evidence ?? campaign?.rejected_jobs ?? 0);
  const operationallyFailedJobs = Number(campaign?.operationally_failed_jobs ?? campaign?.failed_jobs ?? 0);
  const promotedSingleMarketJobs = Number(campaign?.promoted_single_market_jobs ?? 0);
  const queued = Number(campaign?.queued_jobs ?? jobsExecuted);
  const successRate = jobsExecuted ? promotedSingleMarketJobs / jobsExecuted : 0;
  const lifecycleCounts = campaign?.candidate_lifecycle_counts ?? {};
  const paper = simplePaperSummary(snapshot);
  const failures = simpleFailureReasons(snapshot, researchIntelligence, campaign);
  const funnel = simpleFunnel(snapshot, campaign, generated, tested);
  const readiness = authoritativeReadiness(snapshot);
  const campaignState = String(campaign?.status ?? "unavailable");
  const eligibleForwardTrades = Number(snapshot.forward_evidence?.eligible_closed_trades ?? 0);
  const eligibleForwardProfit = numericOrNull(snapshot.forward_evidence?.eligible_profit ?? snapshot.forward_evidence?.eligible_pnl);

  return (
    <div className="pageStack simpleDashboard">
      <PageTitle
        title="KefTrade Dashboard"
        description="Simple Mode summarizes research status, candidate quality, simulated paper performance, and Phase 10 readiness from the same backend evidence used by Advanced Mode."
        actions={<Link className="button ghost" href="/research-intelligence">Open candidates</Link>}
      />

      <section className="simpleHero">
        <div>
          <span className="sectionLabel">Current State</span>
          <h2>{simpleHeadline(promotedSingleMarketJobs, evidenceRejectedJobs, readiness.state, campaignState)}</h2>
          <p>Phase 10 remains locked until an elite candidate exists and eligible candidate-linked forward evidence passes readiness gates.</p>
        </div>
        <div className="phaseProgress">
          {[
            ["Platform", "Complete", "success"],
            ["Research", titleCase(campaignState), campaignState === "completed" ? "success" : campaignState === "running" ? "warning" : "neutral"],
            ["Elite Strategy", Number(lifecycleCounts.elite_candidate ?? 0) ? "Found" : "Not Found", Number(lifecycleCounts.elite_candidate ?? 0) ? "success" : "warning"],
            ["Forward Validation", eligibleForwardTrades ? "Collecting" : "Waiting", "neutral"],
            ["Phase 10", readiness.state === "ready_for_phase_10" ? "Ready" : "Locked", readiness.state === "ready_for_phase_10" ? "success" : "error"]
          ].map(([label, value, toneName]) => <span key={label} className={`phaseStep ${toneName}`}><small>{label}</small><strong>{value}</strong></span>)}
        </div>
      </section>

      <Card title="Campaign Selector" eyebrow="Authoritative source">
        <label className="simpleSelect">
          <span>Campaign</span>
          <select value={selectedCampaignId} onChange={(event) => setSelectedCampaignId(event.target.value)}>
            {campaignOptions.map((option) => <option key={option.id} value={String(option.id)}>{option.label}</option>)}
          </select>
        </label>
        <p className="formHint">Default is the latest completed research campaign. Full Simple Mode metrics use the authoritative completed-campaign summary.</p>
      </Card>

      <section className="simpleGrid">
        <Card title="Research Scans" eyebrow="Campaign status">
          <div className="simpleMetricGrid">
            <SimpleMetric label="Jobs executed" value={jobsExecuted} />
            <SimpleMetric label="Promoted single-market jobs" value={promotedSingleMarketJobs} tone="success" />
            <SimpleMetric label="Jobs rejected by evidence" value={evidenceRejectedJobs} tone={evidenceRejectedJobs ? "warning" : "success"} />
            <SimpleMetric label="Operationally failed jobs" value={operationallyFailedJobs} tone={operationallyFailedJobs ? "error" : "success"} />
            <SimpleMetric label="Success rate" value={percent(successRate)} />
          </div>
          <SimpleProgress label={String(campaign?.name ?? campaign?.campaign_key ?? "Current campaign")} value={jobsExecuted} total={queued || jobsExecuted} />
        </Card>

        <Card title="Best Candidate" eyebrow={candidateStatus(bestCandidate)}>
          <CandidateSummaryCard row={bestCandidate} />
        </Card>
      </section>

      <section className="simpleGrid">
        <Card title="Paper Performance" eyebrow="Simulation only">
          <div className="simpleMetricGrid threeCol">
            <SimpleMetric label="Legacy simulation profit" value={money(paper.totalProfit)} tone={paper.totalProfit >= 0 ? "success" : "warning"} />
            <SimpleMetric label="Today's legacy profit" value={money(paper.todayProfit)} tone={paper.todayProfit >= 0 ? "success" : "warning"} />
            <SimpleMetric label="Legacy orders" value={paper.orders} />
            <SimpleMetric label="Legacy fills" value={paper.fills} />
            <SimpleMetric label="Eligible forward trades" value={eligibleForwardTrades} />
            <SimpleMetric label="Eligible forward profit" value={eligibleForwardTrades ? money(eligibleForwardProfit ?? 0) : "unavailable"} />
            <SimpleMetric label="Phase 10 evidence" value={eligibleForwardTrades ? "collecting" : "not started"} />
            <SimpleMetric label="Eligible profit factor" value={paper.profitFactor === null ? "n/a" : number(paper.profitFactor, 2)} />
            <SimpleMetric label="Eligible expectancy" value={paper.expectancy === null ? "n/a" : money(paper.expectancy)} />
          </div>
        </Card>

        <Card title="Profit Breakdown" eyebrow="Simulated PnL">
          <div className="breakdownGrid">
            <SimpleBreakdown title="Asset" rows={profitByAsset(snapshot)} />
            <SimpleBreakdown title="Strategy" rows={profitByStrategy(snapshot, researchIntelligence)} />
            <SimpleBreakdown title="Market Regime" rows={profitByRegime(snapshot, researchIntelligence)} />
          </div>
        </Card>
      </section>

      <section className="simpleGrid">
        <Card title="Scan Statistics" eyebrow="Worker health">
          <div className="simpleMetricGrid">
            <SimpleMetric label="Generated candidates" value={generated} />
            <SimpleMetric label="Tested candidates" value={tested} />
            <SimpleMetric label="Jobs executed" value={jobsExecuted} />
            <SimpleMetric label="Operationally failed jobs" value={operationallyFailedJobs} tone={operationallyFailedJobs ? "error" : "success"} />
            <SimpleMetric label="Average scan duration" value={formatDuration(snapshot.research_campaigns?.average_job_runtime_ms)} />
            <SimpleMetric label="Average candidate quality" value={formatMaybeNumber(researchIntelligence?.summary?.average_research_score ?? campaign?.average_validation_score)} />
            <SimpleMetric label="Jobs waiting" value={snapshot.research_campaigns?.queue_depth ?? 0} />
            <SimpleMetric label="Evidence-rejected jobs" value={evidenceRejectedJobs} />
          </div>
        </Card>

        <Card title="Top Failure Reasons" eyebrow="Why scans failed">
          <div className="failureList">
            {failures.length ? failures.map((row) => (
              <details key={row.label} className="failureReason">
                <summary><span>{row.label}</span><strong>{row.count}</strong></summary>
                <p>{row.detail}</p>
              </details>
            )) : <EmptyState title="No failure reasons found." body="Failure details appear after campaign jobs are rejected." />}
          </div>
        </Card>
      </section>

      <section className="simpleGrid">
        <Card title="Campaign Progress" eyebrow="Candidate funnel">
          <SimpleProgress label="Jobs" value={jobsExecuted} total={queued || jobsExecuted} large />
          <div className="simpleMetricGrid threeCol">
            <SimpleMetric label="Research Candidates" value={funnel.researchCandidates} />
            <SimpleMetric label="Elite Candidates" value={funnel.eliteCandidates} tone={funnel.eliteCandidates ? "success" : "warning"} />
            <SimpleMetric label="Needs More Evidence" value={funnel.needsMoreEvidence} />
          </div>
          <div className="researchFunnel">
            {funnel.rows.map((row) => <span key={row.label} className={row.tone}><small>{row.label}</small><strong>{row.value}</strong></span>)}
          </div>
        </Card>

        <Card title="Asset Health" eyebrow="Simple score">
          <div className="assetHealthGrid">
            {simpleAssetHealth(snapshot).map((asset) => (
              <article key={asset.key} className="assetHealthCard">
                <strong>{asset.symbol}</strong>
                <span aria-label={`${asset.rating} out of 5`}>{stars(asset.rating)}</span>
                <small>{asset.detail}</small>
              </article>
            ))}
          </div>
        </Card>
      </section>

      <Card title="Advanced Mode keeps the full platform" eyebrow="Professional interface">
        <div className="advancedSurfaceList">
          {["Mission Control", "Research Intelligence", "Validation diagnostics", "Candidate lineage", "Campaign diagnostics", "Strategy mutations", "Parameter explorer", "Regime explorer", "Audit logs", "SQL-backed evidence", "Evidence explorer", "Deployment diagnostics", "Full tables"].map((item) => <span key={item}>{item}</span>)}
        </div>
        {deploymentError ? <p className="formHint">Deployment diagnostics warning: {deploymentError}</p> : null}
        {deploymentManagement ? <p className="formHint">Deployment diagnostics are loaded and available in Advanced Mode.</p> : null}
      </Card>
    </div>
  );
}

function SimpleMetric({ label, value, tone: metricTone = "neutral" }: { label: string; value: React.ReactNode; tone?: SemanticTone }) {
  return <article className={`simpleMetric ${metricTone}`}><span>{label}</span><strong>{value}</strong></article>;
}

function SimpleProgress({ label, value, total, large = false }: { label: string; value: number; total: number; large?: boolean }) {
  const safeTotal = Math.max(total, value, 1);
  const width = Math.min(100, Math.max(0, (value / safeTotal) * 100));
  return (
    <div className={`simpleProgress ${large ? "large" : ""}`}>
      <div><span>{label}</span><strong>{value} / {safeTotal} Jobs</strong></div>
      <div className="progressTrack"><i style={{ width: `${width}%` }} /></div>
    </div>
  );
}

function CandidateSummaryCard({ row }: { row: any }) {
  if (!row) return <EmptyState title="No candidate selected." body="Research Intelligence will provide the best current candidate when evidence is available." />;
  const metrics = row.metrics ?? row.aggregate_metrics ?? {};
  return (
    <article className="candidateCard">
      <header>
        <div>
          <span>{String(row.strategy ?? row.strategy_family ?? "Strategy")}</span>
          <h3>{String(row.candidate_id ?? row.symbol ?? "Candidate")}</h3>
        </div>
        <strong>{candidateStars(row)}</strong>
      </header>
      <div className="candidateIdentity">
        <span>{String(row.symbol ?? row.asset ?? "AAPL")}</span>
        <span>{String(row.timeframe ?? "1h")}</span>
        <span>{candidateStatus(row)}</span>
      </div>
      <div className="simpleMetricGrid threeCol">
        <SimpleMetric label="Profit Factor" value={formatCandidateMetric(metrics.profit_factor ?? row.profit_factor)} />
        <SimpleMetric label="Expectancy" value={formatCandidateMetric(metrics.expectancy ?? metrics.expectancy_per_trade ?? row.expectancy)} />
        <SimpleMetric label="Trades" value={formatCandidateMetric(metrics.trade_count ?? metrics.number_of_trades ?? row.trade_count, 0)} />
        <SimpleMetric label="Drawdown" value={formatCandidateMetric(metrics.max_drawdown ?? metrics.drawdown ?? row.max_drawdown ?? row.drawdown)} />
        <SimpleMetric label="Stability" value={formatCandidateMetric(metrics.stability ?? row.stability)} />
        <SimpleMetric label="Status" value={candidateStatus(row)} />
      </div>
      <div className="candidatePassFail">
        <span><small>Passed</small><strong>{String(row.symbol ?? "Best asset")}</strong></span>
        <span><small>Failed</small><strong>{candidateWeakness(row)}</strong></span>
      </div>
    </article>
  );
}

function SimpleBreakdown({ title, rows }: { title: string; rows: Array<{ label: string; value: number | null }> }) {
  return (
    <div className="simpleBreakdown">
      <strong>{title}</strong>
      {rows.length ? rows.slice(0, 5).map((row) => (
        <span key={row.label}><small>{row.label}</small><b className={(row.value ?? 0) >= 0 ? "positive" : "negative"}>{row.value === null ? "n/a" : money(row.value)}</b></span>
      )) : <span><small>No data</small><b>n/a</b></span>}
    </div>
  );
}

function authoritativeLatestCompletedCampaign(snapshot: MissionControlSnapshot) {
  return snapshot.research_campaigns?.latest_completed_campaign ?? completedCampaignOptions(snapshot, null)[0] ?? null;
}

function completedCampaignOptions(snapshot: MissionControlSnapshot, latest: any | null) {
  const summarized = Array.isArray(snapshot.research_campaigns?.completed_campaign_summaries) ? snapshot.research_campaigns.completed_campaign_summaries : [];
  const campaigns = summarized.length ? summarized : Array.isArray(snapshot.research_campaigns?.campaigns) ? snapshot.research_campaigns.campaigns : [];
  const options = campaigns
    .filter((row: any) => row?.status === "completed")
    .map((row: any) => ({
      ...row,
      label: campaignOptionLabel(row),
      generated_candidates: row.generated_candidates ?? row.requested_candidates ?? row.analytics?.strategies_generated,
      tested_candidates: row.tested_candidates ?? row.requested_candidates ?? row.analytics?.strategies_generated,
      candidate_lifecycle_counts: row.candidate_lifecycle_counts ?? {},
    }));
  if (latest && !options.some((row: any) => String(row.id) === String(latest.id))) {
    options.unshift({ ...latest, label: campaignOptionLabel(latest) });
  }
  return options.sort((a: any, b: any) => {
    const aTime = new Date(a.completed_at ?? a.updated_at ?? 0).getTime();
    const bTime = new Date(b.completed_at ?? b.updated_at ?? 0).getTime();
    return bTime - aTime || Number(b.id ?? 0) - Number(a.id ?? 0);
  });
}

function campaignOptionLabel(row: any) {
  const name = String(row?.name ?? row?.campaign_key ?? `Campaign ${row?.id ?? ""}`).trim();
  const completed = row?.completed_at ? ` / ${new Date(row.completed_at).toLocaleDateString()}` : "";
  return `${name}${completed}`;
}

function simplePaperSummary(snapshot: MissionControlSnapshot) {
  const equityCurve = snapshot.paper_account.recent_equity_curve ?? [];
  const lastEquity = Number(equityCurve.at(-1)?.equity ?? snapshot.paper_account.equity ?? 0);
  const previousEquity = Number(equityCurve.at(-2)?.equity ?? lastEquity);
  const totalProfit = Number(snapshot.paper_account.realized_pnl ?? 0) + Number(snapshot.paper_account.unrealized_pnl ?? 0);
  return {
    totalProfit,
    todayProfit: Number.isFinite(lastEquity - previousEquity) ? lastEquity - previousEquity : 0,
    orders: snapshot.paper_account.recent_simulated_orders?.length ?? 0,
    fills: snapshot.paper_account.recent_simulated_fills?.length ?? 0,
    winningTrades: Number(snapshot.forward_evidence?.winning_trades ?? snapshot.forward_evidence?.paper_winning_trades ?? 0),
    losingTrades: Number(snapshot.forward_evidence?.losing_trades ?? snapshot.forward_evidence?.paper_losing_trades ?? 0),
    winRate: numericOrNull(snapshot.forward_evidence?.win_rate ?? snapshot.forward_evidence?.paper_win_rate),
    profitFactor: numericOrNull(snapshot.forward_evidence?.profit_factor ?? snapshot.forward_evidence?.paper_profit_factor),
    expectancy: numericOrNull(snapshot.forward_evidence?.expectancy ?? snapshot.forward_evidence?.paper_expectancy)
  };
}

function profitByAsset(snapshot: MissionControlSnapshot) {
  const rows = snapshot.deployments.map((deployment) => ({
    label: String((deployment as any).symbol ?? (deployment as any).asset ?? "Asset"),
    value: numericOrNull((deployment as any).unrealized_pnl ?? (deployment as any).realized_pnl ?? (deployment as any).pnl)
  }));
  if (rows.length) return rows;
  return snapshot.assets.slice(0, 5).map((asset) => ({ label: asset.symbol, value: numericOrNull((asset as any).unrealized_pnl ?? (asset as any).pnl) }));
}

function profitByStrategy(snapshot: MissionControlSnapshot, researchIntelligence: any | null) {
  const rows = new Map<string, number>();
  for (const deployment of snapshot.deployments) {
    const key = String((deployment as any).strategy_name ?? (deployment as any).strategy ?? "Strategy");
    rows.set(key, (rows.get(key) ?? 0) + Number((deployment as any).unrealized_pnl ?? (deployment as any).realized_pnl ?? 0));
  }
  if (rows.size) return [...rows.entries()].map(([label, value]) => ({ label, value }));
  const strategies = researchIntelligence?.strategy_leaderboard ?? [];
  return strategies.slice(0, 5).map((row: any) => ({ label: String(row.strategy ?? row.strategy_family ?? "Strategy"), value: numericOrNull(row.total_pnl ?? row.average_expectancy ?? row.average_composite_score) }));
}

function profitByRegime(snapshot: MissionControlSnapshot, researchIntelligence: any | null) {
  const regimes = researchIntelligence?.regime_leaderboard ?? researchIntelligence?.regime_performance ?? snapshot.research_learning?.regime_performance ?? [];
  if (Array.isArray(regimes) && regimes.length) {
    return regimes.slice(0, 5).map((row: any) => ({ label: String(row.regime ?? row.market_regime ?? row.name ?? "Regime"), value: numericOrNull(row.pnl ?? row.expectancy ?? row.average_expectancy ?? row.score) }));
  }
  return [
    { label: "Bull Trend", value: numericOrNull(snapshot.research_summary?.bull_trend_pnl) },
    { label: "Sideways", value: numericOrNull(snapshot.research_summary?.sideways_pnl) },
    { label: "Low Volatility", value: numericOrNull(snapshot.research_summary?.low_volatility_pnl) }
  ];
}

function simpleFailureReasons(snapshot: MissionControlSnapshot, researchIntelligence: any | null, campaign: any | null): SimpleFailureReason[] {
  const campaignReasons = Array.isArray(campaign?.top_failure_reasons) ? campaign.top_failure_reasons : [];
  if (campaignReasons.length) {
    return campaignReasons.slice(0, 6).map((item: any) => ({
      label: humanFailure(String(item.reason ?? item.failure_reason ?? item.name ?? item)),
      count: Number(item.count ?? item.frequency ?? item.value ?? 1),
      detail: "Evidence rejection reason from the latest completed campaign. Open Advanced Mode for raw rejected-job rows."
    }));
  }
  const raw = [
    ...(Array.isArray(snapshot.research_learning?.recurring_failures) ? snapshot.research_learning?.recurring_failures : []),
    ...(Array.isArray(researchIntelligence?.failure_distribution) ? researchIntelligence.failure_distribution : []),
    ...(Array.isArray(snapshot.research_campaigns?.failure_distribution) ? snapshot.research_campaigns?.failure_distribution : [])
  ];
  const rows = raw.map((item: any) => ({
    label: humanFailure(String(item.reason ?? item.failure_reason ?? item.name ?? item)),
    count: Number(item.count ?? item.frequency ?? item.value ?? 1),
    detail: String(item.detail ?? item.recommended_fix ?? "Open Advanced Mode for the full rejected-job evidence and raw validation rows.")
  }));
  if (rows.length) return rows.slice(0, 6);
  return [
    { label: "Weak Profit Factor", count: Number(snapshot.research_summary?.weak_profit_factor ?? 0), detail: "Profit factor did not meet the preserved validation gate." },
    { label: "Poor Expectancy", count: Number(snapshot.research_summary?.poor_expectancy ?? 0), detail: "Average expected PnL per trade was not positive enough." },
    { label: "Insufficient Trades", count: Number(snapshot.research_summary?.insufficient_trades ?? 0), detail: "The candidate did not produce enough trades for evidence confidence." },
    { label: "Sideways Regime", count: Number(snapshot.research_summary?.fails_in_sideways ?? 0), detail: "Performance weakened during sideways market regimes." },
    { label: "Low Volatility", count: Number(snapshot.research_summary?.fails_in_low_volatility ?? 0), detail: "Performance weakened during low-volatility regimes." }
  ].filter((row) => row.count > 0);
}

function simpleFunnel(snapshot: MissionControlSnapshot, campaign: any, generated: number, tested: number) {
  const lifecycle = campaign?.candidate_lifecycle_counts ?? {};
  const needsMoreEvidence = Number(lifecycle.needs_more_evidence ?? campaign?.needs_more_evidence ?? 0);
  const researchCandidates = Number(lifecycle.research_candidate ?? 0);
  const eliteCandidates = Number(lifecycle.elite_candidate ?? campaign?.elite_candidates ?? 0);
  const rejectedCandidates = Number(lifecycle.rejected ?? campaign?.cross_validation_rejected_candidates ?? 0);
  return {
    needsMoreEvidence,
    researchCandidates,
    eliteCandidates,
    rows: [
      { label: "Generated", value: generated, tone: "neutral" },
      { label: "Tested", value: tested, tone: "info" },
      { label: "Rejected", value: rejectedCandidates, tone: "warning" },
      { label: "Needs More Evidence", value: needsMoreEvidence, tone: "info" },
      { label: "Research Candidate", value: researchCandidates, tone: "success" },
      { label: "Elite Candidate", value: eliteCandidates, tone: eliteCandidates ? "success" : "warning" },
      { label: "Paper Validation", value: Number(snapshot.forward_evidence?.candidate_linked_deployments ?? 0), tone: "neutral" },
      { label: "Forward Evidence", value: Number(snapshot.forward_evidence?.eligible_closed_trades ?? 0), tone: "neutral" }
    ]
  };
}

function simpleAssetHealth(snapshot: MissionControlSnapshot) {
  const assets = snapshot.assets.length ? snapshot.assets : ["AAPL", "NVDA", "GOOGL", "LLY", "AVGO"].map((symbol) => ({ symbol }));
  return assets.slice(0, 8).map((asset: any, index) => {
    const freshness = String(asset.data_freshness ?? asset.status ?? asset.health_status ?? "unknown").toLowerCase();
    const opportunities = Number(asset.research_opportunities ?? asset.setup_count ?? 0);
    const rating = freshness.includes("fresh") || freshness.includes("healthy") ? 4 + Number(opportunities > 0) : freshness.includes("warning") ? 3 : freshness.includes("stale") ? 2 : 3;
    const symbol = String(asset.symbol ?? "Asset");
    const detail = String(asset.timeframe ?? asset.asset_class ?? asset.data_freshness ?? "Research coverage");
    return {
      key: `${symbol}-${detail}-${index}`,
      symbol,
      rating: Math.max(1, Math.min(5, rating)),
      detail
    };
  });
}

function candidateStatus(row: any) {
  const raw = String(row?.classification ?? row?.lifecycle_status ?? row?.validation_status ?? row?.status ?? "Research Candidate");
  if (!row) return "No Candidate";
  if (raw.toLowerCase().includes("elite")) return "Elite Candidate";
  if (raw.toLowerCase().includes("needs")) return "Needs More Evidence";
  if (raw.toLowerCase().includes("reject")) return "Rejected";
  return raw === "unknown" ? "Research Candidate" : titleCase(raw.replaceAll("_", " "));
}

function candidateStars(row: any) {
  const score = Number(row?.research_score ?? row?.review_priority_score ?? 0);
  const rating = Math.max(1, Math.min(5, Math.ceil(score / 20) || 3));
  return stars(rating);
}

function candidateWeakness(row: any) {
  const text = String(row?.weakest_dimension ?? row?.review_priority_reason ?? row?.ranking_reason ?? "Cross Asset / Cross Timeframe / Stability");
  if (text.length > 48) return "Cross Asset / Stability";
  return text;
}

function simpleHeadline(successful: number, failed: number, readinessState: string, campaignState = "unavailable") {
  if (readinessState === "ready_for_phase_10") return "An elite candidate is ready for internal paper validation.";
  if (successful > 0) return "Research found promising candidates, but Phase 10 is still locked.";
  if (failed > 0 && campaignState === "completed") return "The latest completed campaign found no elite candidate.";
  if (failed > 0) return `Research campaign state is ${titleCase(campaignState)} with no elite candidate yet.`;
  return "Research is waiting for the next completed campaign.";
}

function formatDuration(value: unknown) {
  const ms = Number(value ?? 0);
  if (!Number.isFinite(ms) || ms <= 0) return "n/a";
  if (ms < 1000) return `${number(ms, 0)} ms`;
  return `${number(ms / 1000, 1)} s`;
}

function stars(value: number) {
  return `${"★".repeat(value)}${"☆".repeat(Math.max(0, 5 - value))}`;
}

function numericOrNull(value: unknown) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function humanFailure(value: string) {
  return titleCase(value.replaceAll("_", " ").replaceAll("fails in", "").replaceAll("poor", "Poor").replaceAll("weak", "Weak"));
}

function titleCase(value: string) {
  return value.replace(/\w\S*/g, (word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase());
}

function QuickActions() {
  const actions = [
    ["/research-intelligence", "Candidate Review", Sparkles],
    ["/paper", "Paper Lab", WalletCards],
    ["/reports", "Reports", LineChartIcon],
    ["/experiments", "Experiments", FlaskConical],
    ["/copilot", "AI Copilot", Bot],
    ["/validation", "Validation", ListChecks],
    ["/paper/deployments", "Deployments", BriefcaseBusiness]
  ] as const;
  return (
    <div className="quickActions">
      {actions.map(([href, label, Icon]) => <Link key={href} href={href}><Icon size={15} /> {label}</Link>)}
    </div>
  );
}

function ExecutiveKpi({ icon, label, value, detail, tone: cardTone }: { icon: React.ReactNode; label: string; value: React.ReactNode; detail: string; tone: SemanticTone }) {
  return (
    <article className={`executiveKpi ${cardTone}`}>
      <span className="kpiIcon">{icon}</span>
      <span className="kpiLabel">{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

type SemanticTone = "success" | "warning" | "error" | "info" | "neutral";

function PriorityCard({ icon, label, value, detail, tone: cardTone, href }: { icon: React.ReactNode; label: string; value: string; detail: string; tone: SemanticTone; href: string }) {
  return (
    <Link className={`priorityCard ${cardTone}`} href={href}>
      <span>{icon}{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </Link>
  );
}

function StatusPill({ label, value }: { label: string; value: string }) {
  return <span className={`statusPill ${semanticTone(value)}`}><small>{label}</small><strong>{value}</strong></span>;
}

function StatusDot({ label, value, tone: dotTone }: { label: string; value: string; tone: SemanticTone }) {
  return <span className={`statusDot ${dotTone}`}><i />{label}<strong>{value}</strong></span>;
}

function SectionHeader({ eyebrow, title, description }: { eyebrow: string; title: string; description?: string }) {
  return (
    <header className="missionSectionHeader">
      <span className="sectionLabel">{eyebrow}</span>
      <h2>{title}</h2>
      {description ? <p>{description}</p> : null}
    </header>
  );
}

function CircularScore({ value, label }: { value: number; label: string }) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div className="circularScore" style={{ background: `conic-gradient(var(--accent) ${clamped * 3.6}deg, var(--panel-soft) 0deg)` }}>
      <div>
        <strong>{number(clamped, 0)}</strong>
        <span>{label}</span>
      </div>
    </div>
  );
}

function SignalItem({ label, value, tone: itemTone }: { label: string; value: string; tone: SemanticTone }) {
  return <span className={`signalItem ${itemTone}`}><small>{label}</small><strong>{value}</strong></span>;
}

function FeaturedCandidate({ row }: { row: any }) {
  if (!row) {
    return <EmptyState title="No ranked candidate yet." body="Run validation to rank candidates." action={<Link className="button" href="/validation">Open Validation</Link>} />;
  }
  return (
    <article className="featuredCandidate">
      <span className="eyebrow">BEST CANDIDATE</span>
      <h3>{String(row.candidate_id ?? "candidate")}</h3>
      <div className="candidateMeta">
        <span>{String(row.symbol ?? "n/a")} / {String(row.timeframe ?? "n/a")}</span>
        <span>{String(row.strategy ?? "unknown strategy")}</span>
        <span>Score {formatMaybeNumber(row.research_score)}</span>
      </div>
      <p>{String(row.ranking_reason ?? row.review_priority_reason ?? "Ranked by evidence quality.")}</p>
      <Link className="tableLink" href="/research-intelligence">Review candidate <ArrowUpRight size={12} /></Link>
    </article>
  );
}

function CompactBars({ title, rows }: { title: string; rows: Array<{ label: string; value: number; tone?: SemanticTone }> }) {
  const max = Math.max(...rows.map((row) => row.value), 1);
  return (
    <div className="compactBars">
      <strong>{title}</strong>
      {rows.length ? rows.map((row) => (
        <div className={`compactBar ${row.tone ?? "info"}`} key={row.label}>
          <span>{row.label}</span>
          <div><i style={{ width: `${Math.max(5, (row.value / max) * 100)}%` }} /></div>
          <b>{row.value}</b>
        </div>
      )) : <span className="muted">No data yet</span>}
    </div>
  );
}

function PriorityRow({ label, value, tone: rowTone }: { label: string; value: string; tone: SemanticTone }) {
  return <span className={`priorityRow ${rowTone}`}><small>{label}</small><strong>{value}</strong></span>;
}

function DiagnosticRows({ rows, empty }: { rows: Array<Record<string, any>>; empty: string }) {
  const visible = rows.filter(Boolean).slice(0, 6);
  if (!visible.length) {
    return <EmptyState title={empty} body="No action required." />;
  }
  return (
    <div className="diagnosticRows">
      {visible.map((row, index) => (
        <article key={`${row.source ?? row.name}-${index}`} className={`diagnosticRow ${semanticTone(String(row.severity ?? row.status ?? ""))}`}>
          <span><AlertTriangle size={14} /> {String(row.source ?? row.name ?? "system")}</span>
          <strong>{String(row.detail ?? row.status ?? "Needs review")}</strong>
          {row.recommended_fix ? <small>{String(row.recommended_fix)}</small> : null}
          {row.timestamp ? <time>{formatDate(row.timestamp)}</time> : null}
        </article>
      ))}
    </div>
  );
}

function GateRows({ gates }: { gates: Array<Record<string, any>> }) {
  if (!Array.isArray(gates) || !gates.length) {
    return <EmptyState title="Readiness data unavailable." body="Phase 10 is not allowed until gates can be verified." />;
  }
  const ordered = [...gates].sort((a, b) => Number(b.mandatory && !b.passed) - Number(a.mandatory && !a.passed));
  return (
    <div className="diagnosticRows">
      {ordered.map((gate) => (
        <article key={gate.name} className={`diagnosticRow ${gate.passed ? "success" : gate.mandatory ? "error" : "warning"}`}>
          <span>{String(gate.name).replaceAll("_", " ")} / {gate.mandatory ? "mandatory" : "informational"}</span>
          <strong>{gate.passed ? "Passed" : "Failed"}</strong>
          <small>Current: {String(gate.current_value ?? "unknown")} / Required: {String(gate.required_value ?? "unknown")}</small>
          <small>{String(gate.failure_reason ?? gate.detail ?? "")}</small>
          <small>{String(gate.recommended_fix ?? "")}</small>
          <time>{formatDate(gate.evaluated_at)}</time>
        </article>
      ))}
    </div>
  );
}

function readinessDiagnostics(gates: any): Array<Record<string, any>> {
  return Array.isArray(gates)
    ? gates.filter((gate) => !gate?.passed).map((gate) => ({
      source: gate.name,
      severity: gate.mandatory ? "warning" : "info",
      detail: gate.detail,
      recommended_fix: gate.recommended_fix
    }))
    : [];
}

function healthDiagnostics(checks: any): Array<Record<string, any>> {
  return Array.isArray(checks)
    ? checks.filter((check) => !check?.passed).map((check) => ({
      source: check.source,
      severity: check.severity,
      detail: check.detail,
      recommended_fix: check.recommended_fix,
      timestamp: check.timestamp
    }))
    : [];
}

function gateProgressRows(gates: any): Array<{ label: string; value: number; tone?: SemanticTone }> {
  if (!Array.isArray(gates) || !gates.length) return [];
  const passed = gates.filter((gate) => gate?.passed).length;
  const failed = gates.length - passed;
  const mandatoryFailed = gates.filter((gate) => gate?.mandatory && !gate?.passed).length;
  return [
    { label: "Passed", value: passed, tone: "success" },
    { label: "Failed", value: failed, tone: failed ? "warning" : "success" },
    { label: "Mandatory", value: mandatoryFailed, tone: mandatoryFailed ? "error" : "success" }
  ];
}

function forwardEvidenceRows(evidence: any): Array<{ label: string; value: number; tone?: SemanticTone }> {
  if (!evidence) return [];
  return [
    { label: "Days", value: Number(evidence.active_validation_days ?? 0), tone: "info" },
    { label: "Eligible", value: Number(evidence.eligible_closed_trades ?? evidence.closed_trades ?? 0), tone: Number(evidence.eligible_closed_trades ?? evidence.closed_trades ?? 0) ? "info" : "warning" },
    { label: "Excluded", value: Number(evidence.excluded_closed_trades ?? 0), tone: Number(evidence.excluded_closed_trades ?? 0) ? "warning" : "success" },
    { label: "All sim", value: Number(evidence.all_simulation_closed_trades ?? 0), tone: "neutral" },
    { label: "Scans", value: Number(evidence.completed_scans ?? 0), tone: "info" },
    { label: "Linked orders", value: Number(evidence.candidate_linked_orders ?? 0), tone: Number(evidence.candidate_linked_orders ?? 0) ? "success" : "neutral" },
    { label: "Linked fills", value: Number(evidence.candidate_linked_fills ?? 0), tone: Number(evidence.candidate_linked_fills ?? 0) ? "success" : "neutral" }
  ];
}

function DetailsPanel({ title, children, defaultOpen = false }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  return (
    <details className="detailsPanel" open={defaultOpen}>
      <summary>{title}<span>Details</span></summary>
      <div>{children}</div>
    </details>
  );
}

function ResearchQueueAndDailySummary({ snapshot }: { snapshot: MissionControlSnapshot }) {
  return (
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
        ) : <EmptyState title="Review queue is empty." body="No attention items." />}
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
  );
}

function SchedulerAndDataHealth({ snapshot }: { snapshot: MissionControlSnapshot }) {
  return (
    <Card title="Scheduler and Data Health" eyebrow="Freshness">
      <div className="dashboardGrid wideLeft">
        <div className="scoreList">
          <SummaryLine label="Scheduler enabled" value={snapshot.system_health.scheduler_status === "Disabled" ? "Disabled" : "Enabled"} />
          <SummaryLine label="Cadence" value={snapshot.system_health.scheduler_cadence ?? "unknown"} />
          <SummaryLine label="Last run" value={formatDate(snapshot.system_health.last_successful_scheduler_run)} />
          <SummaryLine label="Next run" value={formatDate(snapshot.system_health.next_scheduled_scan)} />
          <SummaryLine label="Latest result" value={snapshot.system_health.overall_status} />
          <SummaryLine label="Scheduler failures" value={snapshot.system_health.scheduler_failures} />
          <SummaryLine label="Duplicate candle skips" value={snapshot.system_health.duplicate_candle_skips} />
        </div>
        <DataTable
          columns={["Asset", "Latest candle", "Expected timeframe", "Freshness", "Detail"]}
          rows={snapshot.assets.slice(0, 10).map((asset) => [asset.symbol, formatDate(asset.latest_candle_timestamp), asset.timeframe, <StatusBadge key="freshness" status={asset.data_freshness} />, asset.data_freshness_detail])}
        />
      </div>
    </Card>
  );
}

function MultiAssetResearchTable({ snapshot }: { snapshot: MissionControlSnapshot }) {
  return (
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
      ) : <EmptyState title="No monitored assets yet." body="Assets will appear after ingestion." />}
    </Card>
  );
}

function CampaignWorkerDetails({ snapshot }: { snapshot: MissionControlSnapshot }) {
  return (
    <Card title="Campaign Worker Scheduler" eyebrow="Large-scale research">
      <div className="metricGrid">
        <MetricCard label="Scheduler" value={snapshot.research_campaigns?.scheduler_enabled ? "Enabled" : "Disabled"} tone={snapshot.research_campaigns?.scheduler_enabled ? "success" : "neutral"} />
        <MetricCard label="Queue depth" value={snapshot.research_campaigns?.queue_depth ?? 0} />
        <MetricCard label="Active workers" value={snapshot.research_campaigns?.active_worker_count ?? 0} detail={`${snapshot.research_campaigns?.healthy_worker_count ?? 0} healthy / ${snapshot.research_campaigns?.stale_worker_count ?? 0} stale`} tone={Number(snapshot.research_campaigns?.stale_worker_count ?? 0) ? "warning" : "success"} />
        <MetricCard label="Running jobs" value={snapshot.research_campaigns?.running_jobs ?? 0} />
        <MetricCard label="Retrying jobs" value={snapshot.research_campaigns?.retrying_jobs ?? 0} tone={Number(snapshot.research_campaigns?.retrying_jobs ?? 0) ? "warning" : "neutral"} />
        <MetricCard label="Blocked data" value={snapshot.research_campaigns?.blocked_data_jobs ?? 0} tone={Number(snapshot.research_campaigns?.blocked_data_jobs ?? 0) ? "warning" : "success"} />
        <MetricCard label="Deferred jobs" value={snapshot.research_campaigns?.deferred_jobs ?? 0} />
        <MetricCard label="Failed jobs" value={snapshot.research_campaigns?.failed_jobs ?? 0} tone={Number(snapshot.research_campaigns?.failed_jobs ?? 0) ? "error" : "success"} />
        <MetricCard label="Completed 24h" value={snapshot.research_campaigns?.jobs_completed_last_24h ?? 0} />
        <MetricCard label="Elite promoted" value={snapshot.research_campaigns?.promoted_candidates ?? metric(snapshot, "elite_candidates_promoted")} />
        <MetricCard label="Worker utilization" value={percent(snapshot.research_campaigns?.worker_utilization ?? 0)} />
        <MetricCard label="Campaign efficiency" value={percent(snapshot.research_campaigns?.campaign_efficiency ?? 0)} />
      </div>
      <p className="formHint">ETA: {snapshot.research_campaigns?.campaign_eta ?? "Unavailable"}</p>
      {snapshot.research_campaigns?.campaigns?.length ? (
        <DataTable
          columns={["Campaign", "Status", "Jobs", "Promoted", "Rejected", "Updated"]}
          rows={snapshot.research_campaigns.campaigns.slice(0, 6).map((campaign: Record<string, any>) => [
            String(campaign.name ?? campaign.id),
            String(campaign.status ?? "unknown"),
            `${campaign.completed_jobs ?? 0}/${campaign.queued_jobs ?? 0}`,
            String(campaign.promoted_candidates ?? 0),
            String(campaign.rejected_candidates ?? 0),
            formatDate(campaign.updated_at)
          ])}
        />
      ) : <EmptyState title="No research campaigns queued." body="Campaigns will appear here." />}
    </Card>
  );
}

function PaperLedgerDetails({ snapshot }: { snapshot: MissionControlSnapshot }) {
  return (
    <div className="dashboardGrid">
      <Card title="Recent simulated orders" eyebrow="Paper-only">
        {snapshot.paper_account.recent_simulated_orders.length ? (
          <DataTable columns={["Order", "Symbol", "Side", "Type", "Status", "Submitted"]} rows={snapshot.paper_account.recent_simulated_orders.slice(0, 8).map((order) => [order.id, order.symbol, order.side, order.order_type, order.status, formatDate(order.submitted_at)])} />
        ) : <EmptyState title="No simulated orders." body="No paper orders yet." />}
      </Card>
      <Card title="Recent simulated fills" eyebrow="Paper-only">
        {snapshot.paper_account.recent_simulated_fills.length ? (
          <DataTable columns={["Fill", "Symbol", "Side", "Qty", "Price", "Filled"]} rows={snapshot.paper_account.recent_simulated_fills.slice(0, 8).map((fill) => [fill.id, fill.symbol, fill.side, number(fill.quantity), money(fill.fill_price), formatDate(fill.filled_at)])} />
        ) : <EmptyState title="No simulated fills." body="No paper fills yet." />}
      </Card>
    </div>
  );
}

function FullActivityTimeline({ snapshot }: { snapshot: MissionControlSnapshot }) {
  return (
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
                {item.link ? <Link className="tableLink" href={item.link}>Open <ArrowUpRight size={12} /></Link> : null}
              </div>
            </article>
          ))}
        </div>
      ) : <EmptyState title="No recent activity." body="No events yet." />}
    </Card>
  );
}

function DeploymentManagementCenter({ snapshot, compact = false }: { snapshot: DeploymentManagementSnapshot; compact?: boolean }) {
  const activeIds = snapshot.deployments.filter((deployment) => deployment.status === "active").map((deployment) => deployment.id);
  const risk = snapshot.portfolio_risk;
  return (
    <>
          <Card title="Deployment Control Center" eyebrow="Simulation portfolio">
        <div className="metricGrid">
          <MetricCard label="Deployments" value={snapshot.summary.deployment_count} detail={`${snapshot.summary.active_count} active / ${snapshot.summary.paused_count} paused`} />
          <MetricCard label="Healthy" value={snapshot.summary.healthy_count} tone={Number(snapshot.summary.error_count) ? "error" : Number(snapshot.summary.warning_count) ? "warning" : "success"} detail={`${snapshot.summary.warning_count} warning / ${snapshot.summary.error_count} error`} />
          <MetricCard label="Conflicts" value={snapshot.summary.conflict_count} tone={Number(snapshot.summary.conflict_count) ? "warning" : "success"} />
          <MetricCard label="Gross exposure" value={percent(risk.gross_exposure_pct)} detail="Exposure / equity" tone={Number(risk.exposure_limit_breaches) ? "error" : "neutral"} />
          {!compact ? (
            <>
              <MetricCard label="Open positions" value={risk.open_positions} detail="Long-only" />
              <MetricCard label="Sim equity" value={money(risk.equity)} detail="Paper portfolio" />
              <MetricCard label="Unrealized PnL" value={money(risk.unrealized_pnl)} detail="Simulated" />
              <MetricCard label="Realized PnL" value={money(risk.realized_pnl)} detail="Simulated" />
            </>
          ) : null}
        </div>
        <BulkDeploymentControls activeIds={activeIds} />
        <p className="formHint">{snapshot.safety}</p>
      </Card>

      {!compact ? (
        <>
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
            ) : <EmptyState title="No simulation deployments yet." body="Create one in Paper Lab." action={<Link className="button" href="/paper">Open Paper Lab</Link>} />}
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
                <DataTable columns={["Symbol", "Qty", "Market value", "Unrealized PnL"]} rows={risk.top_positions.map((position) => [position.symbol, number(position.quantity), money(position.market_value), money(position.unrealized_pnl)])} />
              ) : <EmptyState title="No open simulated positions." body="No paper exposure." />}
            </Card>
            <Card title="Conflict Detection" eyebrow="Deployment overlap">
              {snapshot.conflicts.length ? (
                <div className="warningList">
                  {snapshot.conflicts.slice(0, 10).map((conflict, index) => <span key={`${conflict.deployment_id}-${conflict.type}-${index}`}><AlertTriangle size={14} /> {conflict.severity}: {conflict.message}</span>)}
                </div>
              ) : <EmptyState title="No deployment conflicts detected." body="No overlaps found." />}
            </Card>
          </div>
          <div className="dashboardGrid">
            <Card title="Asset Comparison" eyebrow="Deployment performance">
              {snapshot.asset_comparison.length ? <ComparisonTable rows={snapshot.asset_comparison} /> : <EmptyState title="No asset comparison yet." body="No deployments grouped." />}
            </Card>
            <Card title="Strategy Comparison" eyebrow="Deployment performance">
              {snapshot.strategy_comparison.length ? <ComparisonTable rows={snapshot.strategy_comparison} /> : <EmptyState title="No strategy comparison yet." body="No strategies grouped." />}
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
            ) : <EmptyState title="No deployment audit events." body="No control events yet." />}
          </Card>
        </>
      ) : null}
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

function authoritativeReadiness(snapshot: MissionControlSnapshot) {
  const fallbackGates = snapshot.production_validation?.readiness_gates ?? [];
  const blocking = Array.isArray(fallbackGates) ? fallbackGates.filter((gate: any) => gate?.mandatory && !gate?.passed) : [];
  return snapshot.readiness ?? {
    state: snapshot.production_validation?.phase10_readiness_state ?? "unknown",
    score: snapshot.production_validation?.phase10_readiness_score ?? null,
    phase_10_allowed: false,
    blocking_gate_count: blocking.length || snapshot.production_validation?.blocking_readiness_gates?.length || 0,
    blocking_gates: blocking,
    passed_gates: Array.isArray(fallbackGates) ? fallbackGates.filter((gate: any) => gate?.passed) : [],
    gates: fallbackGates,
    last_assessed_at: snapshot.production_validation?.last_readiness_assessment_at ?? snapshot.generated_at
  };
}

function authoritativeCampaign(snapshot: MissionControlSnapshot) {
  return snapshot.campaign ?? {
    state: snapshot.production_validation?.current_validation_campaign?.status ?? "unavailable",
    id: snapshot.production_validation?.current_validation_campaign?.id,
    name: snapshot.production_validation?.current_validation_campaign?.config?.name,
    queue_depth: snapshot.research_campaigns?.queue_depth,
    running_jobs: snapshot.research_campaigns?.running_jobs,
    blocked_data_jobs: snapshot.research_campaigns?.blocked_data_jobs,
    completed_jobs: snapshot.research_campaigns?.completed_jobs,
    failed_jobs: snapshot.research_campaigns?.failed_jobs,
    completed_last_24h: snapshot.research_campaigns?.jobs_completed_last_24h
  };
}

function authoritativeForwardEvidence(snapshot: MissionControlSnapshot) {
  return snapshot.forward_evidence ?? snapshot.production_validation?.forward_evidence ?? {};
}

function activeSubsystemDiagnostics(snapshot: MissionControlSnapshot) {
  const active = snapshot.diagnostics?.active;
  return Array.isArray(active) ? active.filter((row: any) => row?.active !== false) : snapshot.subsystem_errors ?? [];
}

function metric(snapshot: MissionControlSnapshot, key: string) {
  return snapshot.research_summary[key] ?? 0;
}

function metricNumber(snapshot: MissionControlSnapshot, key: string) {
  return Number(metric(snapshot, key) ?? 0);
}

function formatDate(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "Never";
}

function dataAge(value?: string | null) {
  if (!value) return "unknown";
  const ageMs = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(ageMs) || ageMs < 0) return "unknown";
  const minutes = Math.floor(ageMs / 60000);
  if (minutes < 1) return "under 1m";
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function formatMaybeNumber(value?: string | number | null) {
  if (value === null || value === undefined || value === "") return "n/a";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(2) : String(value);
}

function formatCandidateMetric(value?: string | number | null, digits = 4) {
  if (value === null || value === undefined || value === "") return "n/a";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : String(value);
}

function percent(value?: string | number | null) {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? `${(numeric * 100).toFixed(1)}%` : "0.0%";
}

function tone(status: MissionControlStatus | string): SemanticTone {
  if (status === "Healthy" || status === "Protected") return "success";
  if (status === "Error" || status === "Critical" || status === "Avoid") return "error";
  if (status === "Warning" || status === "Stale" || status === "Stale Data" || status === "Disabled") return "warning";
  return "neutral";
}

function readinessTone(state: string): SemanticTone {
  if (state === "ready_for_phase_10") return "success";
  if (state === "conditionally_ready") return "warning";
  if (state === "blocked") return "error";
  return "warning";
}

function semanticTone(status: string): SemanticTone {
  const normalized = status.toLowerCase();
  if (normalized.includes("healthy") || normalized.includes("protected") || normalized.includes("ready_for")) return "success";
  if (normalized.includes("error") || normalized.includes("critical") || normalized.includes("blocked")) return "error";
  if (normalized.includes("warning") || normalized.includes("stale") || normalized.includes("not_ready") || normalized.includes("disabled")) return "warning";
  return "info";
}

function statusTone(status: string) {
  return semanticTone(status);
}

function readinessLabel(state: string, score: number) {
  return `${state.replaceAll("_", " ")} / ${number(score, 0)}`;
}

function executiveHeadline(snapshot: MissionControlSnapshot) {
  if (snapshot.system_health.overall_status === "Error") return "Critical research operations need attention";
  if (snapshot.system_health.overall_status === "Warning" || snapshot.system_health.overall_status === "Stale") return "Research operations are running with attention items";
  if (snapshot.production_validation?.phase10_readiness_state === "ready_for_phase_10") return "Research engine is healthy and Phase 10 gates are clear";
  return "Research engine is online and simulation protected";
}

function buildDashboardDistributions(snapshot: MissionControlSnapshot, researchIntelligence: any | null) {
  const assetsByClass = countBy(snapshot.assets, (asset) => asset.asset_class || "unknown");
  const statusCounts = countBy(snapshot.assets, (asset) => asset.status || "unknown");
  const strategies = countBy(snapshot.deployments, (deployment) => deployment.strategy || "unknown");
  const rankings = researchIntelligence?.rankings ?? [];
  const candidateStatus = countBy(rankings, (row: any) => String(row.classification ?? "unranked"));
  const campaignProgress = [
    { label: "Queued", value: Number(snapshot.research_campaigns?.queue_depth ?? 0), tone: "info" as SemanticTone },
    { label: "Running", value: Number(snapshot.research_campaigns?.running_jobs ?? 0), tone: "info" as SemanticTone },
    { label: "Completed", value: Number(snapshot.research_campaigns?.completed_jobs ?? 0), tone: "success" as SemanticTone },
    { label: "Blocked", value: Number(snapshot.research_campaigns?.blocked_data_jobs ?? 0), tone: "warning" as SemanticTone },
    { label: "Failed", value: Number(snapshot.research_campaigns?.failed_jobs ?? 0), tone: "error" as SemanticTone }
  ];
  const alertTrend = [
    { label: "Review queue", value: snapshot.review_queue.length, tone: "warning" as SemanticTone },
    { label: "Active alerts", value: snapshot.system_health.unacknowledged_alert_count, tone: snapshot.system_health.unacknowledged_alert_count ? "warning" as SemanticTone : "success" as SemanticTone },
    { label: "Scheduler failures", value: Number(snapshot.daily_summary.scheduler_errors ?? 0), tone: Number(snapshot.daily_summary.scheduler_errors ?? 0) ? "error" as SemanticTone : "success" as SemanticTone },
    { label: "Stale blocks", value: Number(snapshot.daily_summary.stale_data_blocks ?? 0), tone: Number(snapshot.daily_summary.stale_data_blocks ?? 0) ? "warning" as SemanticTone : "success" as SemanticTone }
  ];
  return {
    assetDistribution: mapCounts(assetsByClass, "info"),
    strategyDistribution: mapCounts(strategies, "info"),
    candidateStatus: mapCounts(candidateStatus, "success"),
    assetStatus: mapCounts(statusCounts, "info"),
    campaignProgress,
    alertTrend
  };
}

function countBy<T>(rows: T[], fn: (row: T) => string) {
  return rows.reduce<Record<string, number>>((acc, row) => {
    const key = fn(row);
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
}

function mapCounts(counts: Record<string, number>, fallbackTone: SemanticTone) {
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([label, value]) => ({ label, value, tone: semanticTone(label) === "neutral" ? fallbackTone : semanticTone(label) }));
}
