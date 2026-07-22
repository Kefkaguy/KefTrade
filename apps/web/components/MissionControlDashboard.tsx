"use client";

import Link from "next/link";
import { motion, useReducedMotion } from "framer-motion";
import { CartesianGrid, Line, LineChart, ReferenceLine, XAxis, YAxis } from "recharts";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  CircleGauge,
  Clock3,
  Database,
  HardDrive,
  RadioTower,
  RefreshCw,
  Server,
  ShieldCheck,
  TriangleAlert,
  UsersRound
} from "lucide-react";
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart";
import type { DeploymentManagementSnapshot, MissionControlSnapshot, MissionControlStatus, ResearchIntelligence } from "@/lib/api";

type MissionControlProps = {
  snapshot: MissionControlSnapshot;
  deploymentManagement: DeploymentManagementSnapshot | null;
  deploymentError?: string | null;
  researchIntelligence: ResearchIntelligence | null;
};

type Issue = {
  title: string;
  detail: string;
  source: string;
  severity: "warning" | "error";
  action?: string;
};

export function MissionControlDashboard({ snapshot, deploymentManagement, deploymentError, researchIntelligence }: MissionControlProps) {
  const reduceMotion = useReducedMotion();
  const issues = collectIssues(snapshot, deploymentManagement, deploymentError);
  const overallTone = statusTone(snapshot.system_health.overall_status);
  const workers = snapshot.workers ?? {};
  const activeWorkerCount = numberValue(workers.active_worker_count ?? workers.active ?? snapshot.research_campaigns?.active_worker_count);
  const healthyWorkerCount = numberValue(workers.healthy_worker_count ?? workers.healthy ?? snapshot.research_campaigns?.healthy_worker_count);
  const workerStatus: MissionControlStatus = activeWorkerCount > 0 && healthyWorkerCount < activeWorkerCount ? "Warning" : "Healthy";
  const databaseIssue = snapshot.subsystem_errors.find((item) => item.subsystem.toLowerCase().includes("database"));
  const databaseStatus: MissionControlStatus = databaseIssue ? "Error" : "Healthy";
  const readiness = snapshot.readiness;
  const broker = snapshot.external_broker_paper;
  const eliteActivity = broker?.elite_activity ?? [];
  const reveal = reduceMotion ? undefined : { hidden: { opacity: 0, y: 12 }, visible: { opacity: 1, y: 0 } };

  return (
    <motion.div className="pageContainer missionWorkspace" initial="hidden" animate="visible" transition={{ staggerChildren: reduceMotion ? 0 : 0.07 }}>
      <motion.header className="pageIntro missionIntro" variants={reveal}>
        <div><span className="eyebrow">Mission Control</span><h1>System health at a glance.</h1><p>Research infrastructure, data freshness, and automation status without the operational noise.</p></div>
        <div className="pageActions"><span className="lastUpdated"><Clock3 size={14} /> Updated {relativeTime(snapshot.generated_at, snapshot.generated_at)}</span><Link className="button secondary compact" href="/mission-control"><RefreshCw size={15} /> Refresh</Link></div>
      </motion.header>

      <motion.section className={`healthHero ${overallTone}`} variants={reveal}>
        <div className="healthHeroIcon">{issues.length ? <TriangleAlert size={26} /> : <CheckCircle2 size={26} />}</div>
        <div><span className="sectionLabel">Overall system health</span><h2>{healthHeadline(snapshot.system_health.overall_status, issues.length)}</h2><p>{issues.length ? `${issues.length} ${issues.length === 1 ? "item needs" : "items need"} attention. Core research remains simulation protected.` : "All core services are responding normally and research is simulation protected."}</p></div>
        <span className={`statusChip ${overallTone}`}>{snapshot.system_health.overall_status}</span>
      </motion.section>

      <motion.section className="serviceGrid" variants={reveal} aria-label="Core services">
        <ServiceStatus icon={RadioTower} label="API" status={snapshot.system_health.research_engine_status} detail="Research services" />
        <ServiceStatus icon={Database} label="Database" status={databaseStatus} detail={databaseIssue ? "Connection issue" : "Responding normally"} />
        <ServiceStatus icon={UsersRound} label="Workers" status={workerStatus} detail={activeWorkerCount ? `${healthyWorkerCount}/${activeWorkerCount} healthy` : "Ready for queued work"} />
        <ServiceStatus icon={Clock3} label="Scheduler" status={snapshot.system_health.scheduler_status} detail={snapshot.system_health.scheduler_cadence ?? "Cadence unavailable"} />
      </motion.section>

      <motion.div className="missionGrid" variants={reveal}>
        <section className="surface issuesSurface">
          <div className="sectionHeading"><div><span className="eyebrow">Attention</span><h2>{issues.length ? `${issues.length} active ${issues.length === 1 ? "issue" : "issues"}` : "No active issues"}</h2></div>{issues.length ? <span className="issueCount"><AlertTriangle size={14} /> {issues.length}</span> : <CheckCircle2 className="successIcon" size={20} />}</div>
          {issues.length ? (
            <div className="issueList">
              {issues.map((issue) => (
                <details key={`${issue.source}-${issue.title}-${issue.detail}`} className={`issueItem ${issue.severity}`}>
                  <summary><span className="issueIcon"><AlertTriangle size={16} /></span><span><strong>{issue.title}</strong><small>{issue.source}</small></span><span className={`statusChip ${issue.severity}`}>{issue.severity}</span><ChevronDown size={16} /></summary>
                  <div className="issueDetail"><p>{issue.detail}</p>{issue.action ? <span><strong>Recommended:</strong> {issue.action}</span> : null}</div>
                </details>
              ))}
            </div>
          ) : <div className="inlineEmpty healthyEmpty"><ShieldCheck size={24} /><strong>Everything looks clear</strong><span>Diagnostics will stay hidden here until something needs attention.</span></div>}
        </section>

        <section className="surface readinessSurface">
          <div className="sectionHeading"><div><span className="eyebrow">Research readiness</span><h2>{title(readiness?.state ?? "Assessment unavailable")}</h2></div><CircleGauge size={19} /></div>
          <div className="readinessScore"><strong>{Math.round(numberValue(readiness?.score))}</strong><span>/ 100</span></div>
          <div className="progressTrack"><motion.i initial={{ width: 0 }} animate={{ width: `${Math.max(0, Math.min(100, numberValue(readiness?.score)))}%` }} transition={{ duration: reduceMotion ? 0 : 0.85, ease: [0.22, 1, 0.36, 1] }} /></div>
          <div className="compactMetrics twoColumns"><Metric label="Gates passed" value={readiness?.passed_gates?.length ?? 0} /><Metric label="Blocking gates" value={readiness?.blocking_gate_count ?? 0} /></div>
          <Link className="surfaceAction" href="/validation">Review validation gates <ArrowRight size={15} /></Link>
        </section>
      </motion.div>

      <motion.section variants={reveal}>
        <div className="sectionHeading sectionHeadingOutside"><div><span className="eyebrow">Operational overview</span><h2>Current workload</h2></div></div>
        <div className="metricCardGrid">
          <OverviewMetric icon={HardDrive} label="Data freshness" value={snapshot.system_health.overall_data_freshness} detail={`${snapshot.asset_count ?? snapshot.assets.length} monitored assets`} tone={statusTone(snapshot.system_health.overall_data_freshness)} />
          <OverviewMetric icon={Activity} label="Active deployments" value={snapshot.system_health.active_deployment_count} detail={`${snapshot.paper_account.open_positions} open positions`} />
          <OverviewMetric icon={ShieldCheck} label="Safety" value={snapshot.safety.live_routing_enabled ? "Routing enabled" : "Simulation only"} detail={snapshot.safety.detail} tone={snapshot.safety.live_routing_enabled ? "error" : "success"} />
          <OverviewMetric icon={Server} label="Candidates" value={researchIntelligence?.rankings?.length ?? 0} detail="Ranked research records" />
        </div>
      </motion.section>

      <motion.section className="surface activityBand" variants={reveal}>
        <div className="sectionHeading"><div><span className="eyebrow">External paper observation</span><h2>Alpaca Paper foundation</h2></div><span className={`statusChip ${broker?.execution_enabled ? "error" : "success"}`}>{broker?.execution_enabled ? "Execution enabled" : "No order routing"}</span></div>
        <div className="compactMetrics twoColumns">
          <Metric label="Environment" value={broker ? title(broker.environment) : "Unavailable"} />
          <Metric label="Adapter" value={broker?.adapter?.adapter_version ?? "Not synchronized"} />
          <Metric label="Last sync" value={broker?.latest_sync?.status ?? "No snapshot"} />
          <Metric label="Reconciliation" value={broker?.latest_reconciliation?.status ?? "No run"} />
          <Metric label="Observe-only deployments" value={(broker?.deployments ?? []).filter((item) => item.state === "enabled_observe_only").length} />
          <Metric label="Active halts" value={broker?.active_halts?.length ?? 0} />
          <Metric label="Execution epochs" value={broker?.epochs?.length ?? 0} />
          <Metric label="Shadow decisions" value={broker?.shadow_executions?.length ?? 0} />
        </div>
        <OpportunityCoverage coverage={broker?.opportunity_coverage} />
        <p className="surfaceNote">Broker state is read from persisted snapshots. Order submission and external execution remain disabled.</p>
      </motion.section>

      <motion.section className="surface activityBand elitePerformanceSurface" variants={reveal}>
        <div className="sectionHeading">
          <div><span className="eyebrow">Elite performance</span><h2>Today&apos;s observation and historical replay</h2></div>
          <span className="statusChip neutral">{eliteActivity.length} tracked</span>
        </div>
        <p className="surfaceNote">Today&apos;s P&amp;L includes executed Alpaca Paper trades only. Observe-only decisions never count as earnings; replay results are shown separately.</p>
        {eliteActivity.length ? (
          <div className="elitePerformanceBody">
            <ElitePerformanceVisuals elites={eliteActivity} />
            <div className="eliteLedgerHeading"><div><span className="eyebrow">Exact evidence</span><h3>Elite decision ledger</h3></div><span>New York trading day</span></div>
            <div className="tablePanel eliteLedgerTable">
              <table>
                <thead><tr><th>Elite</th><th>Latest decision</th><th>Today</th><th>Paper P&amp;L today</th><th>Historical replay</th><th>Last checked</th></tr></thead>
                <tbody>
                  {eliteActivity.map((elite) => {
                    const replay = elite.historical_replay ?? {};
                    const today = elite.today_performance ?? {};
                    const decision = eliteDecision(elite);
                    return (
                      <tr key={elite.id}>
                        <td><strong>{elite.symbol} <small>{elite.timeframe}</small></strong><small>Elite #{elite.id} · {elite.candidate_id}</small></td>
                        <td><span className={`statusChip ${decision.tone}`}>{decision.label}</span><small>{decision.reason}</small></td>
                        <td><strong>{numberValue(elite.evaluations_today)} unique bars</strong><small>{numberValue(elite.setups_today)} setups · {numberValue(elite.shadow_decisions_today)} shadow decisions · {numberValue(elite.would_submit_today)} would trade</small></td>
                        <td><strong>{today.realized_pnl == null ? "Pending attribution" : money(numberValue(today.realized_pnl))}</strong><small>{numberValue(today.submitted_orders)} submitted orders · {title(String(today.attribution_status ?? "unknown"))}</small></td>
                        <td><strong>{replay.net_pnl == null ? "No completed trades" : money(numberValue(replay.net_pnl))}</strong><small>{replay.profit_factor == null ? "PF —" : `PF ${numberValue(replay.profit_factor).toFixed(3)}`} · {numberValue(replay.completed_trades)} trades · {outcomeHealth(replay)}</small></td>
                        <td><strong>{relativeTime(elite.latest_evaluation_at ?? elite.latest_shadow_at, snapshot.generated_at)}</strong><small>{elite.latest_bar ? `Bar ${formatNewYorkDate(elite.latest_bar)}` : "No evaluated bar"}</small></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ) : <div className="inlineEmpty"><strong>No elite activity available</strong><span>The broker worker will populate this ledger after the next completed cycle.</span></div>}
      </motion.section>

      <motion.section className="surface activityBand" variants={reveal}>
        <div className="sectionHeading"><div><span className="eyebrow">Recent operations</span><h2>Latest system events</h2></div><Link className="textLink" href="/journal">Full journal <ArrowRight size={14} /></Link></div>
        <div className="operationList">
          {snapshot.recent_activity.slice(0, 5).map((item) => (
            <article key={`${item.event_type}-${item.timestamp}-${item.symbol ?? "workspace"}-${item.description}`}><span className={`operationDot ${statusTone(item.status)}`} /><div><strong>{item.description}</strong><small>{item.symbol ? `${item.symbol} / ` : ""}{title(item.event_type)}</small></div><time dateTime={item.timestamp ?? undefined}>{relativeTime(item.timestamp, snapshot.generated_at)}</time></article>
          ))}
          {!snapshot.recent_activity.length ? <div className="inlineEmpty"><strong>No recent operations</strong><span>Scheduler and research events will appear here.</span></div> : null}
        </div>
      </motion.section>
    </motion.div>
  );
}

function ServiceStatus({ icon: Icon, label, status, detail }: { icon: typeof Activity; label: string; status: string; detail: string }) {
  const tone = statusTone(status);
  return <article className={`serviceStatus ${tone}`}><span className="serviceIcon"><Icon size={20} /></span><div><span>{label}</span><strong>{status}</strong><small>{detail}</small></div><i aria-label={`${label} ${status}`} /></article>;
}

function OverviewMetric({ icon: Icon, label, value, detail, tone = "neutral" }: { icon: typeof Activity; label: string; value: unknown; detail: string; tone?: string }) {
  return <article className={`overviewMetric ${tone}`}><span><Icon size={18} /></span><div><small>{label}</small><strong>{String(value ?? 0)}</strong><p>{detail}</p></div></article>;
}

function Metric({ label, value }: { label: string; value: unknown }) {
  return <div><span>{label}</span><strong>{String(value ?? 0)}</strong></div>;
}

function OpportunityCoverage({ coverage }: { coverage?: NonNullable<NonNullable<MissionControlSnapshot["external_broker_paper"]>["opportunity_coverage"]> }) {
  if (!coverage) return null;
  const dominantPercent = Math.round(numberValue(coverage.dominant_symbol_share) * 100);
  return (
    <div className="opportunityCoverage" aria-label="Trading opportunity coverage">
      <div className="opportunityCoverageSummary">
        <span className="eyebrow">Opportunity coverage</span>
        <strong>{title(coverage.classification)}</strong>
        <p>{coverage.unique_symbols} symbols across {coverage.unique_timeframes} timeframes. {coverage.dominant_symbol ?? "No symbol"} represents {dominantPercent}% of active elites.</p>
      </div>
      <div className="coverageMetrics">
        <Metric label="Active elites" value={coverage.active_elites} />
        <Metric label="Unique symbols" value={coverage.unique_symbols} />
        <Metric label="Setup rate today" value={`${(numberValue(coverage.setup_frequency_today) * 100).toFixed(1)}%`} />
        <Metric label="Execution direction" value={coverage.long_only ? "Long only" : "Mixed"} />
      </div>
      <div className="coverageRecommendations">
        {coverage.research_recommendations.map((item) => <span key={item.code}><strong>{title(item.code)}</strong><small>{item.detail}</small></span>)}
      </div>
    </div>
  );
}

function ElitePerformanceVisuals({ elites }: { elites: Array<Record<string, any>> }) {
  const chartRows = elites
    .map((elite) => ({
      id: numberValue(elite.id),
      label: `${String(elite.symbol)} ${String(elite.timeframe)}`,
      pnl: elite.historical_replay?.net_pnl == null ? null : numberValue(elite.historical_replay.net_pnl),
      profitFactor: elite.historical_replay?.profit_factor == null ? null : numberValue(elite.historical_replay.profit_factor),
      trades: numberValue(elite.historical_replay?.completed_trades),
    }))
    .sort((left, right) => (right.pnl ?? Number.NEGATIVE_INFINITY) - (left.pnl ?? Number.NEGATIVE_INFINITY));
  const replayPnl = chartRows.reduce((total, row) => total + (row.pnl ?? 0), 0);
  const completedTrades = chartRows.reduce((total, row) => total + row.trades, 0);
  const checksToday = elites.reduce((total, elite) => total + numberValue(elite.evaluations_today), 0);
  const wouldTradeToday = elites.reduce((total, elite) => total + numberValue(elite.would_submit_today), 0);
  const pnlChartConfig = {
    pnl: { label: "Replay P&L", color: "var(--accent)" },
    missingPnl: { label: "Replay P&L", color: "var(--muted-accent)" },
  } satisfies ChartConfig;
  const factorChartConfig = {
    profitFactor: { label: "Profit factor", color: "var(--accent)" },
    gate: { label: "Promotion gate", color: "var(--muted-accent)" },
    missingProfitFactor: { label: "Profit factor", color: "var(--muted-accent)" },
  } satisfies ChartConfig;
  const chartData = chartRows.map((row) => ({
    ...row,
    missingPnl: row.pnl == null ? 0 : null,
    missingProfitFactor: row.profitFactor == null ? 0 : null,
    gate: 1.2,
  }));

  return (
    <>
      <div className="eliteKpiGrid" aria-label="Elite performance summary">
        <EliteKpi label="Replay net P&L" value={money(replayPnl)} detail={`${completedTrades} completed portfolio trades`} tone={replayPnl >= 0 ? "positive" : "negative"} />
        <EliteKpi label="Elites above PF gate" value={`${chartRows.filter((row) => (row.profitFactor ?? 0) >= 1.2).length} / ${chartRows.length}`} detail="Profit factor at or above 1.20" />
        <EliteKpi label="Unique bars today" value={String(checksToday)} detail="One strategy evaluation per completed candle" />
        <EliteKpi label="Would trade today" value={String(wouldTradeToday)} detail="Shadow decisions passing every gate" tone={wouldTradeToday ? "positive" : "neutral"} />
      </div>

      <div className="eliteChartGrid">
        <section className="eliteChartCard" aria-labelledby="elite-pnl-chart-title">
          <div className="eliteChartHeader"><div><h3 id="elite-pnl-chart-title">Historical replay P&amp;L</h3><p>Net simulated USD after modeled slippage and fees · latest replay</p></div><span className="chartLegend"><i /> Replay P&amp;L <i /> No trades</span></div>
          <ChartContainer config={pnlChartConfig} className="eliteLineChart">
            <LineChart accessibilityLayer data={chartData} margin={{ top: 16, right: 12, left: 0, bottom: 4 }}>
              <CartesianGrid vertical={false} stroke="var(--line)" strokeDasharray="3 5" />
              <XAxis dataKey="label" tickLine={false} axisLine={false} tickMargin={11} minTickGap={12} />
              <YAxis tickLine={false} axisLine={false} width={45} tickFormatter={(value) => `$${value}`} />
              <ReferenceLine y={0} stroke="var(--muted-accent)" strokeDasharray="4 4" />
              <ChartTooltip cursor={{ stroke: "var(--line-strong)", strokeDasharray: "3 3" }} content={<ChartTooltipContent valueFormatter={(value, key) => key === "missingPnl" ? "No completed trades" : money(value)} />} />
              <Line type="monotone" dataKey="pnl" stroke="var(--color-pnl)" strokeWidth={2.25} dot={{ r: 4, fill: "var(--panel)", strokeWidth: 2 }} activeDot={{ r: 6 }} connectNulls={false} />
              <Line type="linear" dataKey="missingPnl" stroke="transparent" strokeWidth={0} dot={{ r: 5, fill: "var(--panel)", stroke: "var(--color-missingPnl)", strokeWidth: 2, strokeDasharray: "2 2" }} activeDot={{ r: 6, fill: "var(--panel)", stroke: "var(--color-missingPnl)", strokeWidth: 2 }} connectNulls={false} />
            </LineChart>
          </ChartContainer>
          <div className="chartEvidenceRow">{chartRows.map((row) => <span key={row.id}><small>Elite #{row.id}</small><strong className={(row.pnl ?? 0) >= 0 ? "positive" : "negative"}>{row.pnl == null ? "—" : signedMoney(row.pnl)}</strong></span>)}</div>
        </section>

        <section className="eliteChartCard" aria-labelledby="elite-pf-chart-title">
          <div className="eliteChartHeader"><div><h3 id="elite-pf-chart-title">Profit factor vs promotion gate</h3><p>Gross winning P&amp;L divided by gross losing P&amp;L · 1.20 required</p></div><span className="benchmarkLabel">Gate 1.20 · ○ no trades</span></div>
          <ChartContainer config={factorChartConfig} className="eliteLineChart">
            <LineChart accessibilityLayer data={chartData} margin={{ top: 16, right: 12, left: 0, bottom: 4 }}>
              <CartesianGrid vertical={false} stroke="var(--line)" strokeDasharray="3 5" />
              <XAxis dataKey="label" tickLine={false} axisLine={false} tickMargin={11} minTickGap={12} />
              <YAxis domain={[0, "auto"]} tickLine={false} axisLine={false} width={35} tickFormatter={(value) => Number(value).toFixed(1)} />
              <ChartTooltip cursor={{ stroke: "var(--line-strong)", strokeDasharray: "3 3" }} content={<ChartTooltipContent valueFormatter={(value, key) => key === "missingProfitFactor" ? "No completed trades" : value.toFixed(3)} />} />
              <Line type="monotone" dataKey="gate" stroke="var(--color-gate)" strokeWidth={1.25} strokeDasharray="5 5" dot={false} activeDot={false} />
              <Line type="monotone" dataKey="profitFactor" stroke="var(--color-profitFactor)" strokeWidth={2.25} dot={{ r: 4, fill: "var(--panel)", strokeWidth: 2 }} activeDot={{ r: 6 }} connectNulls={false} />
              <Line type="linear" dataKey="missingProfitFactor" stroke="transparent" strokeWidth={0} dot={{ r: 5, fill: "var(--panel)", stroke: "var(--color-missingProfitFactor)", strokeWidth: 2, strokeDasharray: "2 2" }} activeDot={{ r: 6, fill: "var(--panel)", stroke: "var(--color-missingProfitFactor)", strokeWidth: 2 }} connectNulls={false} />
            </LineChart>
          </ChartContainer>
          <div className="chartEvidenceRow">{chartRows.map((row) => <span key={row.id}><small>{row.trades} trades</small><strong className={(row.profitFactor ?? 0) >= 1.2 ? "positive" : "negative"}>{row.profitFactor == null ? "—" : row.profitFactor.toFixed(3)}</strong></span>)}</div>
        </section>
      </div>
    </>
  );
}

function EliteKpi({ label, value, detail, tone = "neutral" }: { label: string; value: string; detail: string; tone?: string }) {
  return <article className={`eliteKpi ${tone}`}><span>{label}</span><strong>{value}</strong><small>{detail}</small></article>;
}

function eliteDecision(elite: Record<string, any>) {
  if (elite.latest_would_submit) return { label: "Would trade", tone: "success", reason: "All shadow gates passed" };
  const failedGates = Array.isArray(elite.latest_gates)
    ? elite.latest_gates.filter((gate: Record<string, any>) => gate.status === "failed").map((gate: Record<string, any>) => String(gate.code ?? "failed gate"))
    : [];
  const rejectionReasons = Array.isArray(elite.latest_rejection_reasons) ? elite.latest_rejection_reasons.map(String) : [];
  const reasons = [...new Set([...failedGates, ...rejectionReasons])];
  if (elite.latest_signal) return { label: title(String(elite.latest_signal)), tone: elite.latest_signal === "setup" ? "warning" : "neutral", reason: reasons.slice(0, 3).join(", ") || "No actionable setup" };
  return { label: "Waiting", tone: "neutral", reason: "No completed evaluation yet" };
}

function outcomeHealth(replay: Record<string, any>) {
  if (!replay.health) return "unclassified";
  if (replay.health === "broken" && numberValue(replay.expectancy) > 0) return "below 1.20 PF gate";
  if (replay.health === "broken") return "negative expectancy";
  return title(String(replay.health));
}

function money(value: number) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}

function signedMoney(value: number) {
  return `${value > 0 ? "+" : ""}${money(value)}`;
}

function collectIssues(snapshot: MissionControlSnapshot, deploymentManagement: DeploymentManagementSnapshot | null, deploymentError?: string | null): Issue[] {
  const issues: Issue[] = [];
  for (const item of snapshot.diagnostics?.active ?? []) {
    issues.push({
      title: String(item.title ?? item.error ?? item.subsystem ?? "Operational diagnostic"),
      detail: String(item.detail ?? item.message ?? item.error ?? "An operational check requires attention."),
      source: title(String(item.subsystem ?? item.source ?? "System diagnostic")),
      severity: String(item.severity ?? item.status ?? "warning").toLowerCase().includes("error") ? "error" : "warning",
      action: item.action ? String(item.action) : undefined
    });
  }
  for (const item of snapshot.subsystem_errors) {
    issues.push({ title: `${title(item.subsystem)} unavailable`, detail: item.error, source: "Subsystem", severity: "error", action: "Check the service connection and retry the failed operation." });
  }
  if (snapshot.system_health.scheduler_failures > 0) {
    issues.push({ title: "Scheduler failures detected", detail: `${snapshot.system_health.scheduler_failures} scheduler ${snapshot.system_health.scheduler_failures === 1 ? "failure was" : "failures were"} reported in the current snapshot.`, source: "Scheduler", severity: "warning", action: "Review the activity journal and retry the affected jobs." });
  }
  if (["Warning", "Stale", "Error"].includes(snapshot.system_health.overall_data_freshness)) {
    issues.push({ title: "Market data needs attention", detail: "One or more monitored assets have stale or incomplete candle data.", source: "Market data", severity: snapshot.system_health.overall_data_freshness === "Error" ? "error" : "warning", action: "Open Data Coverage to inspect affected assets." });
  }
  if (deploymentError) {
    issues.push({ title: "Deployment management unavailable", detail: deploymentError, source: "Forward validation", severity: "warning", action: "Refresh after the deployment service recovers." });
  } else if (deploymentManagement && numberValue(deploymentManagement.summary.error_count) > 0) {
    issues.push({ title: "Deployment health errors", detail: `${deploymentManagement.summary.error_count} deployment records report an error state.`, source: "Forward validation", severity: "error", action: "Open Forward Validation and inspect deployment health." });
  }
  for (const halt of snapshot.external_broker_paper?.active_halts ?? []) {
    issues.push({ title: `External paper ${title(String(halt.scope_type ?? "broker"))} halt`, detail: String(halt.reason ?? "An external paper safety control is active."), source: "Alpaca Paper", severity: String(halt.severity ?? "warning").toLowerCase() === "critical" ? "error" : "warning", action: "Review the persisted reconciliation evidence and use the audited VPS CLI for any resume." });
  }
  return dedupeIssues(issues).slice(0, 8);
}

function dedupeIssues(issues: Issue[]) {
  const seen = new Set<string>();
  return issues.filter((issue) => {
    const key = `${issue.source}:${issue.title}`.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function statusTone(status: string) {
  const normalized = status.toLowerCase();
  if (["healthy", "protected", "ready", "complete", "active"].some((value) => normalized.includes(value))) return "success";
  if (["error", "critical", "failed", "blocked"].some((value) => normalized.includes(value))) return "error";
  if (["warning", "stale", "disabled", "paused"].some((value) => normalized.includes(value))) return "warning";
  return "neutral";
}

function healthHeadline(status: string, issueCount: number) {
  if (statusTone(status) === "success" && !issueCount) return "All systems operational";
  if (statusTone(status) === "error") return "Core services need attention";
  return "Operational with attention items";
}

function numberValue(value: unknown) {
  const result = Number(value ?? 0);
  return Number.isFinite(result) ? result : 0;
}

function title(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function relativeTime(value?: string | null, referenceValue?: string | null) {
  if (!value) return "Recently";
  const timestamp = new Date(value).getTime();
  const reference = referenceValue ? new Date(referenceValue).getTime() : timestamp;
  const minutes = Math.max(0, Math.floor((reference - timestamp) / 60000));
  if (!Number.isFinite(minutes) || minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
  return `${Math.floor(minutes / 1440)}d ago`;
}

function formatNewYorkDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "numeric",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}
