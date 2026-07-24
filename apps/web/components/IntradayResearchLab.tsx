"use client";

import { useEffect, useState } from "react";
import { Archive, CheckCircle2, Circle, Clock, ShieldAlert, TrendingUp } from "lucide-react";
import { getIntradayLabOverview, type IntradayLabOverview, type IntradaySampleJob } from "@/lib/api";
import { Card, EmptyState, PageTitle } from "@/components/ResearchUI";

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

export function IntradayResearchLab() {
  const [overview, setOverview] = useState<IntradayLabOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTimeframe, setActiveTimeframe] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getIntradayLabOverview()
      .then((result) => {
        if (!active) return;
        setOverview(result);
        setActiveTimeframe(result.timeframes_supported[0] ?? null);
      })
      .catch((reason) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "Could not load the Intraday Research Lab.");
      });
    return () => {
      active = false;
    };
  }, []);

  const selectedBreakdown = overview?.timeframe_breakdown.find((row) => row.timeframe === activeTimeframe) ?? null;
  const archivedStrategies = (overview?.strategies ?? []).filter((s) => s.status === "archived");
  const plannedStrategies = (overview?.strategies ?? []).filter((s) => s.status === "planned");

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
                <em className={`familyTag ${strategy.status === "archived" ? "muted" : "warn"}`}>
                  {strategy.status === "archived" ? "Archived" : strategy.status === "planned" ? "Planned" : "Active"}
                </em>
              </header>
              {strategy.status === "archived" ? (
                <>
                  <p className="intradayStrategyReason">{strategy.reason}</p>
                  {strategy.summary ? <p className="intradayStrategySummary">{strategy.summary}</p> : null}
                  {strategy.jobs != null ? (
                    <div className="intradayStrategyStats">
                      <span>{strategy.campaigns ?? 0} campaign{(strategy.campaigns ?? 0) === 1 ? "" : "s"}</span>
                      <span>{(strategy.trades ?? 0).toLocaleString()} trades</span>
                      <span>{strategy.promoted ?? 0} promoted</span>
                    </div>
                  ) : null}
                </>
              ) : (
                <p className="intradayStrategyPlaceholder"><Circle size={12} /> Not started — no code, no evidence yet.</p>
              )}
            </article>
          ))}
          {!overview ? <EmptyState title="Loading strategy roster" body="Reading the Intraday Lab overview." /> : null}
        </div>
      </Card>

      {overview?.pilot ? (
        <Card title="Pilot results" eyebrow={`Campaign ${overview.pilot.campaign_id}`}>
          <div className="metricGrid intradayPilotMetrics">
            <div className="metricCard neutral"><span>Simulated trades</span><strong>{overview.pilot.trades.toLocaleString()}</strong></div>
            <div className="metricCard neutral"><span>Jobs completed</span><strong>{overview.pilot.jobs.toLocaleString()}</strong></div>
            <div className="metricCard neutral"><span>Promoted</span><strong>{overview.pilot.promoted}</strong></div>
            <div className="metricCard warning"><span>Outcome</span><strong>Archived (negative)</strong></div>
          </div>
          <p className="intradayPilotNote"><ShieldAlert size={14} /> Simulation only. This campaign never placed a live or paper order.</p>
        </Card>
      ) : null}

      <Card title="Timeframe intelligence" eyebrow="Click a timeframe">
        <div className="intradayTimeframeTabs" role="tablist">
          {(overview?.timeframe_breakdown ?? []).map((row) => (
            <button
              key={row.timeframe}
              type="button"
              role="tab"
              aria-selected={activeTimeframe === row.timeframe}
              className={`intradayTimeframeTab ${activeTimeframe === row.timeframe ? "selected" : ""}`}
              onClick={() => setActiveTimeframe(row.timeframe)}
            >
              <strong>{row.timeframe}</strong>
              <span>PF {num(row.avg_profit_factor, 2)}</span>
            </button>
          ))}
        </div>

        {selectedBreakdown ? (
          <div className="intradayTimeframeDetail">
            <div className="intradayTimeframeDetailGrid">
              <div><span>Strategies tested</span><strong>{archivedStrategies.map((s) => `${s.name}${s.version ? ` ${s.version}` : ""}`).join(", ") || "None"}</strong></div>
              <div><span>Jobs</span><strong>{selectedBreakdown.jobs.toLocaleString()}</strong></div>
              <div><span>Trades</span><strong>{selectedBreakdown.trades.toLocaleString()}</strong></div>
              <div><span>Avg profit factor</span><strong>{num(selectedBreakdown.avg_profit_factor)}</strong></div>
              <div><span>Avg expectancy</span><strong>{num(selectedBreakdown.avg_expectancy)}</strong></div>
              <div><span>Status</span><strong>{selectedBreakdown.status === "archived" ? "Archived as negative evidence" : "Not started"}</strong></div>
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
        ) : (
          <EmptyState title="No timeframe data yet" body="Run an intraday campaign to populate this breakdown." />
        )}
      </Card>

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
        </div>
      </Card>

      {overview?.sample_jobs?.length ? (
        <Card title="Rejected candidates" eyebrow="Sample from the archive">
          <div className="intradayCandidateList">
            {overview.sample_jobs.map((job, index) => (
              <IntradayCandidateRow key={`${job.symbol}-${job.timeframe}-${job.direction}-${job.buffer_level}-${index}`} job={job} />
            ))}
          </div>
        </Card>
      ) : null}

      <div className="intradayHonestyBanner">
        <TrendingUp size={16} />
        <span>{overview?.forward_validation_note ?? "Intraday research available. No validated intraday strategy currently approved for forward validation."}</span>
      </div>
    </div>
  );
}

function IntradayCandidateRow({ job }: { job: IntradaySampleJob }) {
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
          <div><span>Family</span><strong>Opening Range Breakout</strong></div>
          <div><span>Timeframe</span><strong>{job.timeframe}</strong></div>
          <div><span>Direction</span><strong className="capitalize">{job.direction ?? "—"}</strong></div>
          <div><span>Breakout buffer</span><strong>{job.buffer_level ?? "—"}</strong></div>
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
