"use client";

import { useEffect, useMemo, useState } from "react";
import { Archive, CheckCircle2, Circle, Rocket, ShieldAlert, TrendingUp } from "lucide-react";
import {
  getIntradayCampaignPlan,
  getIntradayExpansionRecommendation,
  getIntradayFamilyDiagnostics,
  getIntradayLabOverview,
  launchIntradayBroadScreen,
  launchLowTimeframeExpansion,
  type IntradayBroadScreenResult,
  type IntradayCampaignPlan,
  type IntradayExpansionRecommendation,
  type IntradayFamilyDiagnostic,
  type IntradayLabOverview,
  type IntradaySampleJob,
  type IntradayStrategyRosterEntry
} from "@/lib/api";
import { Card, EmptyState, PageTitle } from "@/components/ResearchUI";
import { Phase124Panel } from "@/components/Phase124Panel";
import { StrategyIntelligencePanel } from "@/components/StrategyIntelligencePanel";

const REASON_LABELS: Record<string, string> = {
  weak_profit_factor: "Weak profit factor",
  poor_expectancy: "Poor expectancy",
  insufficient_trades: "Insufficient trades",
  high_drawdown: "High drawdown",
  fails_in_unknown: "Fails in unknown regimes",
  frequency_too_low: "Frequency too low"
};

function reasonLabel(reason: string) {
  return REASON_LABELS[reason] ?? reason.replaceAll("_", " ");
}

function num(value: number | null | undefined, digits = 2) {
  return value == null ? "—" : value.toFixed(digits);
}

function weightedAverage(breakdown: IntradayStrategyRosterEntry["timeframe_breakdown"], field: "avg_profit_factor" | "avg_expectancy") {
  const rows = (breakdown ?? []).filter((row) => row[field] != null && row.trades > 0);
  const totalTrades = rows.reduce((sum, row) => sum + row.trades, 0);
  if (!totalTrades) return null;
  const weighted = rows.reduce((sum, row) => sum + (row[field] as number) * row.trades, 0);
  return weighted / totalTrades;
}

function timeframeSummary(strategies: IntradayStrategyRosterEntry[], timeframe: string) {
  const rows = strategies.flatMap((strategy) =>
    (strategy.timeframe_breakdown ?? [])
      .filter((row) => row.timeframe === timeframe)
      .map((row) => ({ strategy, row }))
  );
  const jobs = rows.reduce((sum, item) => sum + item.row.jobs, 0);
  const trades = rows.reduce((sum, item) => sum + item.row.trades, 0);
  const promotedFamilies = rows.filter((item) => (item.strategy.promoted ?? 0) > 0 && item.row.jobs > 0).length;
  const activeFamilies = rows.filter((item) => item.row.status === "has_evidence" && item.row.jobs > 0).length;
  const totalTrades = rows.reduce((sum, item) => sum + item.row.trades, 0);
  const avgProfitFactor = totalTrades
    ? rows.reduce((sum, item) => sum + (item.row.avg_profit_factor ?? 0) * item.row.trades, 0) / totalTrades
    : null;
  const avgExpectancy = totalTrades
    ? rows.reduce((sum, item) => sum + (item.row.avg_expectancy ?? 0) * item.row.trades, 0) / totalTrades
    : null;
  const leaders = rows
    .filter((item) => item.row.jobs > 0)
    .sort((a, b) => ((b.strategy.promoted ?? 0) - (a.strategy.promoted ?? 0)) || ((b.row.avg_profit_factor ?? 0) - (a.row.avg_profit_factor ?? 0)))
    .slice(0, 4);
  return { timeframe, jobs, trades, promotedFamilies, activeFamilies, avgProfitFactor, avgExpectancy, leaders };
}

function strategySignal(strategy: IntradayStrategyRosterEntry) {
  const promoted = strategy.promoted ?? 0;
  const trades = strategy.trades ?? 0;
  const avgProfitFactor = weightedAverage(strategy.timeframe_breakdown, "avg_profit_factor");
  if (promoted > 0) return "promoted";
  if (trades > 0 && avgProfitFactor != null && avgProfitFactor >= 1) return "near";
  if (strategy.status === "archived") return "archived";
  return "quiet";
}

export function IntradayResearchLab() {
  const [overview, setOverview] = useState<IntradayLabOverview | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    getIntradayLabOverview()
      .then(setOverview)
      .catch((reason) => setError(reason instanceof Error ? reason.message : "Could not load the Intraday Research Lab."));
  };

  useEffect(() => {
    let active = true;
    getIntradayLabOverview()
      .then((result) => {
        if (active) setOverview(result);
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "Could not load the Intraday Research Lab.");
      });
    return () => {
      active = false;
    };
  }, []);

  const archivedStrategies = (overview?.strategies ?? []).filter((s) => s.status === "archived");
  const plannedStrategies = (overview?.strategies ?? []).filter((s) => s.status === "planned");
  const testedStrategies = (overview?.strategies ?? []).filter((s) => s.pilot);
  const strategies = overview?.strategies ?? [];
  const timeframeLanes = (overview?.timeframes_supported ?? ["15m", "30m"]).map((tf) => timeframeSummary(strategies, tf));
  const materialStrategies = strategies
    .filter((strategy) => (strategy.jobs ?? 0) > 0)
    .sort((a, b) => ((b.promoted ?? 0) - (a.promoted ?? 0)) || ((b.trades ?? 0) - (a.trades ?? 0)))
    .slice(0, 8);
  const phase124FamilyIds = new Set([
    "gap_fill_v1",
    "session_momentum_v1",
    "intraday_trend_pullback_v1",
    "ema_trend_continuation_v1",
    "opening_fade_v1",
    "vwap_trend_continuation_v1"
  ]);
  const phase124CampaignId =
    (overview?.strategies ?? []).find((s) => phase124FamilyIds.has(s.id) && s.pilot)?.pilot?.campaign_id ?? null;
  // Most recent campaign with evidence: what the Phase F diagnostics and the
  // expansion recommendation are read from.
  const latestCampaignId = strategies.reduce<number | null>((latest, strategy) => {
    const id = strategy.pilot?.campaign_id ?? null;
    return id != null && (latest == null || id > latest) ? id : latest;
  }, null);

  return (
    <div className="pageContainer">
      <PageTitle
        title="Intraday Research Lab"
        description="Session-aware research on 15m/30m bars: structural flat-by-session-close enforcement, opening-range/VWAP/relative-volume features, and the same honest elite gate every swing candidate goes through."
      />

      <section className="intradayStatusRow">
        <div className="intradayStatusBadge complete">
          <CheckCircle2 size={16} /> Infrastructure Complete
        </div>
        <div className="intradayTimeframePills">
          {(overview?.timeframes_supported ?? ["15m", "30m"]).map((tf) => (
            <span key={tf} className="intradayPill">{tf}</span>
          ))}
        </div>
      </section>

      {error ? <div className="strategyLibraryError" role="alert">{error}</div> : null}

      <Card title="15m / 30m evidence lanes" eyebrow="What matters now">
        <div className="intradayLaneGrid">
          {timeframeLanes.map((lane) => (
            <article key={lane.timeframe} className="intradayLaneCard">
              <header>
                <strong>{lane.timeframe}</strong>
                <em className={`familyTag ${lane.promotedFamilies > 0 ? "good" : lane.jobs > 0 ? "warn" : "muted"}`}>
                  {lane.promotedFamilies > 0 ? `${lane.promotedFamilies} signal families` : lane.jobs > 0 ? "Needs edge" : "No evidence"}
                </em>
              </header>
              <div className="intradayLaneMetrics">
                <div><span>Jobs</span><strong>{lane.jobs.toLocaleString()}</strong></div>
                <div><span>Trades</span><strong>{lane.trades.toLocaleString()}</strong></div>
                <div><span>Avg PF</span><strong>{num(lane.avgProfitFactor)}</strong></div>
                <div><span>Expectancy</span><strong>{num(lane.avgExpectancy)}</strong></div>
              </div>
              {lane.leaders.length ? (
                <ul className="intradayLeaderList">
                  {lane.leaders.map(({ strategy, row }) => (
                    <li key={`${lane.timeframe}-${strategy.id}`}>
                      <span>{strategy.name}</span>
                      <strong>{strategy.promoted ?? 0} promoted</strong>
                    </li>
                  ))}
                </ul>
              ) : <p className="intradayStrategySummary">No jobs recorded on this lane yet.</p>}
            </article>
          ))}
        </div>
      </Card>

      <LaunchIntradayCampaign
        timeframesSupported={overview?.timeframes_supported ?? ["15m", "30m"]}
        latestCampaignId={latestCampaignId}
        onLaunched={load}
      />

      <FamilyRoster strategies={materialStrategies} latestCampaignId={latestCampaignId} loading={!overview} />

      <details className="intradayDeepDive">
        <summary>Show highest-signal family cards</summary>
        <div className="intradayStrategyGrid compact">
          {materialStrategies.map((strategy) => {
            const signal = strategySignal(strategy);
            return (
            <article key={strategy.id} className={`intradayStrategyCard ${strategy.status} ${signal}`}>
              <header>
                <strong>{strategy.name}{strategy.version ? ` ${strategy.version}` : ""}</strong>
                <em className={`familyTag ${strategy.status === "archived" ? "muted" : strategy.status === "planned" ? "warn" : "good"}`}>
                  {strategy.status === "archived" ? "Archived" : strategy.status === "planned" ? "Planned" : "Active"}
                </em>
              </header>
              {strategy.status === "archived" ? (
                <>
                  <p className="intradayStrategyReason">{strategy.reason}</p>
                  {strategy.summary ? <p className="intradayStrategySummary">{strategy.summary}</p> : null}
                </>
              ) : strategy.status === "planned" ? (
                <p className="intradayStrategyPlaceholder"><Circle size={12} /> Not started — no code, no evidence yet.</p>
              ) : (
                <p className="intradayStrategySummary">Implemented, awaiting or under research. No forward validation approved.</p>
              )}
              {strategy.jobs != null ? (
                <div className="intradayStrategyStats">
                  <span>{strategy.campaigns ?? 0} campaign{(strategy.campaigns ?? 0) === 1 ? "" : "s"}</span>
                  <span>{(strategy.trades ?? 0).toLocaleString()} trades</span>
                  <span>{strategy.promoted ?? 0} promoted</span>
                </div>
              ) : null}
            </article>
            );
          })}
          {!overview ? <EmptyState title="Loading strategy roster" body="Reading the Intraday Lab overview." /> : null}
        </div>
      </details>

      <details className="intradayDeepDive">
        <summary>Show detailed family diagnostics</summary>
        <div className="intradayDeepDiveBody">
          {testedStrategies.map((strategy) => (
            <FamilyResearchDetail key={strategy.id} strategy={strategy} />
          ))}
        </div>
      </details>

      <details className="intradayDeepDive">
        <summary>Show legacy Phase 12.4 / strategy intelligence panels</summary>
        <div className="intradayDeepDiveBody">
          <Phase124Panel campaignId={phase124CampaignId} />
          <StrategyIntelligencePanel campaignId={phase124CampaignId} />
        </div>
      </details>

      <Card title="Research archive" eyebrow="Preserved, not deleted">
        <p className="intradayArchiveIntro">
          <Archive size={14} /> Failed intraday research is preserved exactly like swing research — every rejected job's evidence stays queryable, nothing is deleted.
        </p>
        <div className="strategyFamilyTable legacy threeCol" role="table" aria-label="Archived intraday strategies">
          <div role="row" className="strategyFamilyHead">
            <span role="columnheader">Family</span>
            <span role="columnheader">Result</span>
            <span role="columnheader">Reason</span>
          </div>
          {archivedStrategies.map((strategy) => (
            <div role="row" key={strategy.id} className="strategyFamilyRow muted">
              <span role="cell">{strategy.name}{strategy.version ? ` ${strategy.version}` : ""}</span>
              <span role="cell"><em className="familyTag muted">Archived</em></span>
              <span role="cell">{strategy.reason}</span>
            </div>
          ))}
          {plannedStrategies.map((strategy) => (
            <div role="row" key={strategy.id} className="strategyFamilyRow">
              <span role="cell">{strategy.name}</span>
              <span role="cell"><em className="familyTag warn">Planned</em></span>
              <span role="cell">Not implemented yet</span>
            </div>
          ))}
          {!archivedStrategies.length && !plannedStrategies.length ? (
            <div className="strategyFamilyEmpty">No archived or planned families yet.</div>
          ) : null}
        </div>
      </Card>

      <div className="intradayHonestyBanner">
        <TrendingUp size={16} />
        <span>{overview?.forward_validation_note ?? "Intraday research available. No validated intraday strategy currently approved for forward validation."}</span>
      </div>
    </div>
  );
}

const DIAGNOSTIC_LABELS: Record<string, string> = {
  NO_RAW_SIGNAL: "No raw signal",
  COST_DESTROYED_SIGNAL: "Cost destroyed signal",
  WRONG_EXIT_LOGIC: "Exit logic",
  WRONG_DIRECTION: "Wrong direction",
  POOR_REGIME_TARGETING: "Regime targeting",
  ONE_SYMBOL_DEPENDENCE: "One-symbol dependence",
  EXCESSIVE_TURNOVER: "Excessive turnover",
  ENTRY_LATENCY_PROBLEM: "Entry latency",
  PASSED_NO_FAILURE: "Ready for confirmation"
};

function diagnosticLabel(diagnostic: IntradayFamilyDiagnostic | undefined) {
  if (!diagnostic) return null;
  if (diagnostic.recommendation === "single_change_experiment") {
    return `Mutation available — ${DIAGNOSTIC_LABELS[diagnostic.failure_reason ?? ""] ?? diagnostic.failure_reason}`;
  }
  if (diagnostic.recommendation === "advance_to_confirmation") return "Ready for confirmation";
  if (diagnostic.recommendation === "retire") return "No raw signal — retire";
  return DIAGNOSTIC_LABELS[diagnostic.failure_reason ?? ""] ?? null;
}

/** Secondary informational list: name, status, and Phase F state when known. */
function FamilyRoster({
  strategies,
  latestCampaignId,
  loading
}: {
  strategies: IntradayStrategyRosterEntry[];
  latestCampaignId: number | null;
  loading: boolean;
}) {
  const [diagnostics, setDiagnostics] = useState<IntradayFamilyDiagnostic[] | null>(null);

  useEffect(() => {
    let active = true;
    getIntradayFamilyDiagnostics(latestCampaignId).then((result) => {
      if (active) setDiagnostics(result);
    });
    return () => {
      active = false;
    };
  }, [latestCampaignId]);

  const byArchitecture = useMemo(() => {
    const map = new Map<string, IntradayFamilyDiagnostic>();
    for (const row of diagnostics ?? []) map.set(row.architecture, row);
    return map;
  }, [diagnostics]);

  return (
    <Card title="Families" eyebrow="Registry status">
      <div className="strategyFamilyTable legacy threeCol" role="table" aria-label="Intraday strategy families">
        <div role="row" className="strategyFamilyHead">
          <span role="columnheader">Family</span>
          <span role="columnheader">Status</span>
          <span role="columnheader">Diagnostic state</span>
        </div>
        {strategies.map((strategy) => {
          const diagnostic = diagnosticLabel(byArchitecture.get(strategy.id));
          return (
            <div role="row" key={strategy.id} className={`strategyFamilyRow ${strategy.status === "archived" ? "muted" : ""}`}>
              <span role="cell">{strategy.name}{strategy.version ? ` ${strategy.version}` : ""}</span>
              <span role="cell">
                <em className={`familyTag ${strategy.status === "archived" ? "muted" : "good"}`}>
                  {strategy.status === "archived" ? "Archived" : "Active"}
                </em>
              </span>
              <span role="cell">{diagnostic ?? "—"}</span>
            </div>
          );
        })}
        {loading ? <div className="strategyFamilyEmpty">Loading families…</div> : null}
        {!loading && !strategies.length ? <div className="strategyFamilyEmpty">No families with evidence yet.</div> : null}
      </div>
    </Card>
  );
}

function launchLabel(timeframes: string[]) {
  if (!timeframes.length) return "Launch Research";
  return `Launch ${timeframes.join(" / ")} Research`;
}

function LaunchIntradayCampaign({
  timeframesSupported,
  latestCampaignId,
  onLaunched
}: {
  timeframesSupported: string[];
  latestCampaignId: number | null;
  onLaunched: () => void;
}) {
  const [selectedTimeframes, setSelectedTimeframes] = useState<string[]>(timeframesSupported);
  const [plan, setPlan] = useState<IntradayCampaignPlan | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [launched, setLaunched] = useState<IntradayBroadScreenResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Keep the default in sync once the supported list arrives from the backend.
  useEffect(() => {
    setSelectedTimeframes(timeframesSupported);
  }, [timeframesSupported.join(",")]);

  // The plan is the single source of truth for every number shown below; the
  // job count is never recomputed here.
  useEffect(() => {
    let active = true;
    if (!selectedTimeframes.length) {
      setPlan(null);
      return;
    }
    getIntradayCampaignPlan({ timeframes: selectedTimeframes })
      .then((result) => {
        if (active) {
          setPlan(result);
          setPlanError(null);
        }
      })
      .catch((reason) => {
        if (active) {
          setPlan(null);
          setPlanError(reason instanceof Error ? reason.message : "Could not load the campaign plan.");
        }
      });
    return () => {
      active = false;
    };
  }, [selectedTimeframes.join(",")]);

  function toggleTimeframe(tf: string) {
    setSelectedTimeframes((prev) => (prev.includes(tf) ? prev.filter((item) => item !== tf) : [...prev, tf]));
  }

  async function launch(allowRerun = false) {
    if (busy || !selectedTimeframes.length || (plan && !plan.can_launch)) return;
    setBusy(true);
    setError(null);
    setLaunched(null);
    try {
      const result = await launchIntradayBroadScreen({ timeframes: selectedTimeframes, allowRerun });
      setLaunched(result);
      onLaunched();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not launch the research campaign.");
    } finally {
      setBusy(false);
    }
  }

  const blocked = Boolean(plan && !plan.can_launch);
  const disabled = busy || !selectedTimeframes.length || blocked;

  return (
    <Card title="Launch 15m / 30m Research" eyebrow="Broad screen first, exploit winners second">
      <p className="intradayStrategySummary">
        {plan?.evidence_policy ??
          "Each family keeps its own candidates and is evaluated independently through the unmodified elite gate — evidence is never merged across families."}
      </p>

      <div className="intradayTimeframePills" style={{ marginTop: 4 }}>
        {timeframesSupported.map((tf) => (
          <button
            key={tf}
            type="button"
            className={`intradayPill selectable ${selectedTimeframes.includes(tf) ? "selected" : ""}`}
            aria-pressed={selectedTimeframes.includes(tf)}
            onClick={() => toggleTimeframe(tf)}
          >
            {tf}
          </button>
        ))}
      </div>

      {!selectedTimeframes.length ? (
        <p className="intradayStrategyPlaceholder" style={{ marginTop: 12 }}>
          <Circle size={12} /> Select at least one timeframe to launch.
        </p>
      ) : null}

      {plan ? (
        <div className="intradayTimeframeDetailGrid" style={{ marginTop: 16 }}>
          <div><span>Active families</span><strong>{plan.active_family_count}</strong></div>
          <div><span>Assets</span><strong>{plan.asset_count}</strong></div>
          <div><span>Timeframes</span><strong>{plan.timeframes_selected.join(", ") || "—"}</strong></div>
          <div><span>Variants per family</span><strong>{plan.variants_per_family}</strong></div>
          <div><span>Estimated jobs</span><strong>{plan.estimated_jobs.toLocaleString()}</strong></div>
          <div><span>Split protocol</span><strong className="mono">{plan.protocol.split_protocol_version}</strong></div>
          <div><span>Elite gate</span><strong className="mono">{plan.protocol.elite_gate_version}</strong></div>
          <div><span>Cost model</span><strong className="mono">{plan.protocol.cost_model.version}</strong></div>
        </div>
      ) : null}

      {planError ? <div className="strategyLibraryError" role="alert" style={{ marginTop: 14 }}>{planError}</div> : null}

      {plan?.blockers.length ? (
        <div className="strategyLibraryError" role="alert" style={{ marginTop: 14 }}>
          <strong>Launch blocked</strong>
          <ul>
            {plan.blockers.map((item) => (
              <li key={item.code}>{item.detail}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {plan?.warnings.length ? (
        <div className="intradayHonestyBanner" style={{ marginTop: 14 }}>
          <ShieldAlert size={16} />
          <span>{plan.warnings.map((item) => item.detail).join(" ")}</span>
        </div>
      ) : null}

      {plan?.requires_rerun_confirmation ? (
        <div className="intradayHonestyBanner" style={{ marginTop: 14 }}>
          <ShieldAlert size={16} />
          <span>
            This exact configuration already ran as campaign #{plan.duplicate_of_campaign_id}. Re-running
            records a separate campaign — useful once the rolling dataset has advanced, wasted compute if it
            has not.
          </span>
        </div>
      ) : null}

      <button
        type="button"
        className="button"
        style={{ marginTop: 16 }}
        disabled={disabled}
        onClick={() => void launch(Boolean(plan?.requires_rerun_confirmation))}
      >
        <Rocket size={15} />{" "}
        {busy
          ? "Launching…"
          : plan?.requires_rerun_confirmation
            ? `Re-run ${launchLabel(selectedTimeframes).replace(/^Launch /, "")}`
            : launchLabel(selectedTimeframes)}
      </button>

      {error ? <div className="strategyLibraryError" role="alert" style={{ marginTop: 14 }}>{error}</div> : null}

      {launched ? (
        <div className="strategyLibraryNotice" style={{ marginTop: 14 }}>
          <strong>Campaign #{launched.campaign_id} — {launched.campaign?.name ?? "Broad screen"}</strong>
          <ul>
            <li>{launched.plan.active_family_count} families included</li>
            <li>Timeframes: {(launched.timeframes ?? []).join(", ")}</li>
            <li>{(launched.jobs_created ?? 0).toLocaleString()} jobs inserted</li>
            <li>Status: {launched.campaign?.status ?? "queued"}</li>
          </ul>
        </div>
      ) : null}

      <FocusedExpansion campaignId={latestCampaignId} busy={busy} onLaunched={onLaunched} />
    </Card>
  );
}

function FocusedExpansion({
  campaignId,
  busy,
  onLaunched
}: {
  campaignId: number | null;
  busy: boolean;
  onLaunched: () => void;
}) {
  const [recommendation, setRecommendation] = useState<IntradayExpansionRecommendation | null>(null);
  const [running, setRunning] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getIntradayExpansionRecommendation(campaignId).then((result) => {
      if (active) setRecommendation(result);
    });
    return () => {
      active = false;
    };
  }, [campaignId]);

  async function launchExpansion() {
    if (running || busy) return;
    setRunning(true);
    setError(null);
    setNotice(null);
    try {
      const result = await launchLowTimeframeExpansion({});
      setNotice(`Expansion #${result.campaign_id} queued: ${result.jobs_created} jobs from ${result.parent_count} parent rows.`);
      onLaunched();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not launch the focused expansion.");
    } finally {
      setRunning(false);
    }
  }

  const hasRecommendation = Boolean(recommendation?.available);

  return (
    <details className="intradayDeepDive" style={{ marginTop: 20 }}>
      <summary>Focused expansion (secondary)</summary>
      <div className="intradayDeepDiveBody">
        {hasRecommendation ? (
          <p className="intradayStrategySummary">
            Backend recommends expanding: {recommendation!.families.map((f) => f.family_name ?? f.architecture).join(", ")}.
          </p>
        ) : (
          <p className="intradayStrategyPlaceholder">
            <Circle size={12} /> No defensible expansion recommendation from the backend right now.
          </p>
        )}
        <button
          type="button"
          className="button secondary"
          disabled={running || busy}
          onClick={() => void launchExpansion()}
        >
          <Rocket size={15} /> {running ? "Launching…" : "Launch near-pass expansion"}
        </button>
        {error ? <div className="strategyLibraryError" role="alert" style={{ marginTop: 12 }}>{error}</div> : null}
        {notice ? <div className="strategyLibraryNotice" style={{ marginTop: 12 }}>{notice}</div> : null}
      </div>
    </details>
  );
}

function FamilyResearchDetail({ strategy }: { strategy: IntradayStrategyRosterEntry }) {
  const breakdown = strategy.timeframe_breakdown ?? [];
  const [activeTimeframe, setActiveTimeframe] = useState<string | null>(breakdown[0]?.timeframe ?? null);
  const selectedBreakdown = breakdown.find((row) => row.timeframe === activeTimeframe) ?? breakdown[0] ?? null;

  return (
    <Card title={`${strategy.name}${strategy.version ? ` ${strategy.version}` : ""}`} eyebrow={strategy.pilot ? `Campaign ${strategy.pilot.campaign_id}` : "Pilot"}>
      {strategy.pilot ? (
        <>
          <div className="metricGrid intradayPilotMetrics">
            <div className="metricCard neutral"><span>Simulated trades</span><strong>{strategy.pilot.trades.toLocaleString()}</strong></div>
            <div className="metricCard neutral"><span>Jobs completed</span><strong>{strategy.pilot.jobs.toLocaleString()}</strong></div>
            <div className="metricCard neutral"><span>Promoted</span><strong>{strategy.pilot.promoted}</strong></div>
            <div className="metricCard warning"><span>Outcome</span><strong>{strategy.pilot.outcome === "archived_negative_result" ? "Archived (negative)" : "Under review"}</strong></div>
          </div>
          <p className="intradayPilotNote"><ShieldAlert size={14} /> Simulation only. This campaign never placed a live or paper order.</p>
        </>
      ) : null}

      {breakdown.length ? (
        <div className="intradayTimeframeTabs" role="tablist" style={{ marginTop: 20 }}>
          {breakdown.map((row) => (
            <button
              key={row.timeframe}
              type="button"
              role="tab"
              aria-selected={activeTimeframe === row.timeframe}
              className={`intradayTimeframeTab ${(activeTimeframe ?? breakdown[0]?.timeframe) === row.timeframe ? "selected" : ""}`}
              onClick={() => setActiveTimeframe(row.timeframe)}
            >
              <strong>{row.timeframe}</strong>
              <span>PF {num(row.avg_profit_factor, 2)}</span>
            </button>
          ))}
        </div>
      ) : null}

      {selectedBreakdown ? (
        <div className="intradayTimeframeDetail">
          <div className="intradayTimeframeDetailGrid">
            <div><span>Jobs</span><strong>{selectedBreakdown.jobs.toLocaleString()}</strong></div>
            <div><span>Trades</span><strong>{selectedBreakdown.trades.toLocaleString()}</strong></div>
            <div><span>Avg profit factor</span><strong>{num(selectedBreakdown.avg_profit_factor)}</strong></div>
            <div><span>Avg expectancy</span><strong>{num(selectedBreakdown.avg_expectancy)}</strong></div>
            <div><span>Status</span><strong>{selectedBreakdown.status === "has_evidence" ? "Has research evidence" : "Not started"}</strong></div>
          </div>
          {selectedBreakdown.primary_rejection_reasons.length ? (
            <div className="intradayRejectionReasons">
              <span>Primary rejection reasons</span>
              <ul>
                {selectedBreakdown.primary_rejection_reasons.map((reason) => (
                  <li key={reason.reason}>{reasonLabel(reason.reason)} <em>{reason.occurrences}</em></li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}

      {strategy.sample_jobs?.length ? (
        <div style={{ marginTop: 20 }}>
          <span className="sectionLabel">Rejected candidates (sample from the archive)</span>
          <div className="intradayCandidateList" style={{ marginTop: 10 }}>
            {strategy.sample_jobs.map((job, index) => (
              <IntradayCandidateRow key={`${job.symbol}-${job.timeframe}-${job.direction}-${index}`} familyName={strategy.name} job={job} />
            ))}
          </div>
        </div>
      ) : null}
    </Card>
  );
}

function IntradayCandidateRow({ familyName, job }: { familyName: string; job: IntradaySampleJob }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`intradayCandidateRow ${open ? "open" : ""}`}>
      <button type="button" className="intradayCandidateSummary" onClick={() => setOpen((prev) => !prev)} aria-expanded={open}>
        <span className="mono">{job.symbol}</span>
        <span>{job.timeframe}</span>
        <span>{job.direction ?? "—"}</span>
        <span className="mono">{num(job.profit_factor)}</span>
        <em className="familyTag muted">Rejected</em>
      </button>
      {open ? (
        <div className="intradayCandidateDetail">
          <div><span>Family</span><strong>{familyName}</strong></div>
          <div><span>Timeframe</span><strong>{job.timeframe}</strong></div>
          <div><span>Direction</span><strong className="capitalize">{job.direction ?? "—"}</strong></div>
          <div><span>Variant parameter</span><strong>{job.variant_parameter ?? "—"}</strong></div>
          <div><span>Trades</span><strong>{job.trades ?? 0}</strong></div>
          <div><span>Outcome</span><strong>Rejected</strong></div>
          <div className="intradayCandidateWhy">
            <span>Why</span>
            <ul>
              <li>Profit factor {num(job.profit_factor)}</li>
              <li>Expectancy {num(job.expectancy)} per trade</li>
              {job.failure_reasons?.filter((reason) => reason in REASON_LABELS).map((reason) => (
                <li key={reason}>{reasonLabel(reason)}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
