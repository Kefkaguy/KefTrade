"use client";

import { useEffect, useMemo, useState } from "react";
import { Archive, CheckCircle2, Circle, Rocket, ShieldAlert, TrendingUp } from "lucide-react";
import { createIntradayCampaign, getIntradayLabOverview, type IntradayLabOverview, type IntradaySampleJob, type IntradayStrategyRosterEntry } from "@/lib/api";
import { Card, DataTable, EmptyState, PageTitle } from "@/components/ResearchUI";

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

      <Card title="Strategy families" eyebrow="Lifecycle status">
        <div className="intradayStrategyGrid">
          {(overview?.strategies ?? []).map((strategy) => (
            <article key={strategy.id} className={`intradayStrategyCard ${strategy.status}`}>
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
          ))}
          {!overview ? <EmptyState title="Loading strategy roster" body="Reading the Intraday Lab overview." /> : null}
        </div>
      </Card>

      <Card title="Compare families" eyebrow="All families, side by side">
        <DataTable
          columns={["Family", "Status", "Campaigns", "Trades", "Avg PF", "Avg expectancy", "Promoted", "Archive status"]}
          rows={(overview?.strategies ?? []).map((strategy) => [
            `${strategy.name}${strategy.version ? ` ${strategy.version}` : ""}`,
            strategy.status === "archived" ? "Archived" : strategy.status === "planned" ? "Planned" : "Active",
            strategy.campaigns ?? 0,
            (strategy.trades ?? 0).toLocaleString(),
            num(weightedAverage(strategy.timeframe_breakdown, "avg_profit_factor")),
            num(weightedAverage(strategy.timeframe_breakdown, "avg_expectancy")),
            strategy.promoted ?? 0,
            strategy.status === "archived" ? "Archived (negative result)" : strategy.pilot ? "Has evidence" : "No evidence yet"
          ])}
        />
      </Card>

      <LaunchIntradayCampaign strategies={overview?.strategies ?? []} timeframesSupported={overview?.timeframes_supported ?? ["15m", "30m"]} onLaunched={load} />

      {testedStrategies.map((strategy) => (
        <FamilyResearchDetail key={strategy.id} strategy={strategy} />
      ))}

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

function LaunchIntradayCampaign({
  strategies,
  timeframesSupported,
  onLaunched
}: {
  strategies: IntradayStrategyRosterEntry[];
  timeframesSupported: string[];
  onLaunched: () => void;
}) {
  const launchable = useMemo(() => strategies.filter((s) => s.status !== "planned"), [strategies]);
  const [selectedFamilies, setSelectedFamilies] = useState<string[]>([]);
  const [selectedTimeframes, setSelectedTimeframes] = useState<string[]>(timeframesSupported);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  function toggleFamily(id: string) {
    setSelectedFamilies((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));
  }

  function toggleTimeframe(tf: string) {
    setSelectedTimeframes((prev) => {
      const exists = prev.includes(tf);
      if (exists && prev.length === 1) return prev;
      return exists ? prev.filter((item) => item !== tf) : [...prev, tf];
    });
  }

  async function launch() {
    if (!selectedFamilies.length) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await createIntradayCampaign({ familyIds: selectedFamilies, timeframes: selectedTimeframes });
      setNotice(`Campaign #${result.campaign_id} queued: ${result.jobs_created} jobs across ${(result.timeframes || []).join(", ")}, ${(result.assets || []).length} assets.`);
      onLaunched();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not launch the campaign.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Launch an intraday campaign" eyebrow="One, several, or all families">
      <p className="intradayStrategySummary">
        Each selected family keeps its own candidates and is evaluated independently through the unmodified elite gate — evidence is never merged across families.
      </p>
      <div className="intradayFamilyToggleGrid">
        {launchable.map((strategy) => {
          const selected = selectedFamilies.includes(strategy.id);
          return (
            <button
              key={strategy.id}
              type="button"
              className={`intradayFamilyToggle ${selected ? "selected" : ""}`}
              aria-pressed={selected}
              onClick={() => toggleFamily(strategy.id)}
            >
              <strong>{strategy.name}</strong>
              <em className={`familyTag ${strategy.status === "archived" ? "muted" : "good"}`}>{strategy.status === "archived" ? "Archived" : "Active"}</em>
            </button>
          );
        })}
      </div>
      <div className="intradayTimeframePills" style={{ marginTop: 16 }}>
        {timeframesSupported.map((tf) => (
          <button
            key={tf}
            type="button"
            className={`intradayPill selectable ${selectedTimeframes.includes(tf) ? "selected" : ""}`}
            onClick={() => toggleTimeframe(tf)}
          >
            {tf}
          </button>
        ))}
      </div>
      {error ? <div className="strategyLibraryError" role="alert" style={{ marginTop: 14 }}>{error}</div> : null}
      {notice ? <div className="strategyLibraryNotice" style={{ marginTop: 14 }}>{notice}</div> : null}
      <button
        type="button"
        className="button secondary"
        style={{ marginTop: 16 }}
        disabled={busy || !selectedFamilies.length}
        onClick={() => void launch()}
      >
        <Rocket size={15} /> {busy ? "Launching…" : `Launch ${selectedFamilies.length || ""} famil${selectedFamilies.length === 1 ? "y" : "ies"}`}
      </button>
    </Card>
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
