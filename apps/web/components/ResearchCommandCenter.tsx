"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Beaker,
  Copy,
  Filter,
  FlaskConical,
  Layers3,
  RefreshCw,
  Target,
} from "lucide-react";
import {
  fetchResearchCommandCenter,
  type ResearchCommandCenter,
  type ResearchCommandCenterFilters,
} from "@/lib/api";
import { DataTable, EmptyState, MetricCard } from "@/components/ResearchUI";

type SelectFilter = keyof Pick<ResearchCommandCenterFilters, "asset" | "assetClass" | "timeframe" | "strategyFamily" | "candidateState" | "validationRule" | "regime">;

const EMPTY_FILTERS: ResearchCommandCenterFilters = {};

export function ResearchCommandCenterDashboard() {
  const [data, setData] = useState<ResearchCommandCenter | null>(null);
  const [filters, setFilters] = useState<ResearchCommandCenterFilters>(EMPTY_FILTERS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const filterKey = JSON.stringify(filters);

  useEffect(() => {
    let active = true;
    setLoading(true);
    fetchResearchCommandCenter(filters)
      .then((next) => {
        if (!active) return;
        setData(next);
        setError("");
      })
      .catch((reason) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "Research command center unavailable.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [filterKey]);

  const options = data?.filter_options ?? {};
  const activeFilterCount = useMemo(
    () => Object.values(filters).filter((value) => value !== undefined && value !== "").length,
    [filters],
  );

  function updateFilter(key: SelectFilter | "campaignId" | "dateFrom" | "dateTo", value: string) {
    setFilters((current) => ({
      ...current,
      [key]: key === "campaignId" ? (value ? Number(value) : undefined) : value || undefined,
    }));
  }

  if (!data && loading) {
    return <div className="researchLoading"><RefreshCw className="spin" size={18} /> Loading campaign evidence</div>;
  }
  if (!data) {
    return <EmptyState title="Research evidence unavailable" body={error || "No campaign data was returned."} />;
  }

  const overview = data.overview ?? {};
  const duplicates = data.duplicate_analysis ?? {};
  const proposal = data.next_campaign_proposal;

  return (
    <div className="researchCommandCenter">
      <section className="researchFilterBand" aria-label="Research filters">
        <div className="researchFilterTitle">
          <Filter size={16} />
          <span>Evidence scope</span>
          <small>{activeFilterCount ? `${activeFilterCount} active` : "All campaign evidence"}</small>
        </div>
        <div className="researchFilters">
          <FilterSelect label="Campaign" value={filters.campaignId ? String(filters.campaignId) : ""} onChange={(value) => updateFilter("campaignId", value)} options={data.campaigns.map((row) => ({ value: String(row.id), label: String(row.name) }))} />
          <FilterSelect label="Asset" value={filters.asset} onChange={(value) => updateFilter("asset", value)} options={stringOptions(options.assets)} />
          <FilterSelect label="Asset class" value={filters.assetClass} onChange={(value) => updateFilter("assetClass", value)} options={stringOptions(options.asset_classes)} />
          <FilterSelect label="Timeframe" value={filters.timeframe} onChange={(value) => updateFilter("timeframe", value)} options={stringOptions(options.timeframes)} />
          <FilterSelect label="Strategy family" value={filters.strategyFamily} onChange={(value) => updateFilter("strategyFamily", value)} options={stringOptions(options.strategy_families)} />
          <FilterSelect label="Candidate state" value={filters.candidateState} onChange={(value) => updateFilter("candidateState", value)} options={stringOptions(options.candidate_states)} />
          <FilterSelect label="Validation rule" value={filters.validationRule} onChange={(value) => updateFilter("validationRule", value)} options={stringOptions(options.validation_rules)} />
          <FilterSelect label="Regime" value={filters.regime} onChange={(value) => updateFilter("regime", value)} options={stringOptions(options.regimes)} />
          <label className="researchDateFilter"><span>From</span><input type="date" value={filters.dateFrom ?? ""} onChange={(event) => updateFilter("dateFrom", event.target.value)} /></label>
          <label className="researchDateFilter"><span>To</span><input type="date" value={filters.dateTo ?? ""} onChange={(event) => updateFilter("dateTo", event.target.value)} /></label>
          <button className="iconButton" type="button" title="Clear filters" aria-label="Clear filters" onClick={() => setFilters(EMPTY_FILTERS)} disabled={!activeFilterCount}><RefreshCw size={16} /></button>
        </div>
        {loading ? <div className="researchRefresh"><RefreshCw className="spin" size={13} /> Refreshing</div> : null}
      </section>

      <ResearchSection id="overview" title="Research Overview" eyebrow={String(data.campaign?.name ?? "Campaign evidence")} icon={<Activity size={17} />}>
        <div className="metricGrid researchMetrics">
          <MetricCard label="Campaign jobs" value={overview.campaign_jobs ?? 0} />
          <MetricCard label="Candidates generated" value={overview.candidates_generated ?? 0} />
          <MetricCard label="Candidates tested" value={overview.candidates_tested ?? 0} />
          <MetricCard label="Candidates rejected" value={overview.candidates_rejected ?? 0} tone={overview.candidates_rejected ? "error" : "neutral"} />
          <MetricCard label="Candidates completed" value={overview.candidates_completed ?? 0} />
          <MetricCard label="Needs more evidence" value={overview.needs_more_evidence ?? 0} tone={overview.needs_more_evidence ? "warning" : "neutral"} />
          <MetricCard label="Research candidates" value={overview.research_candidates ?? 0} tone={overview.research_candidates ? "success" : "neutral"} />
          <MetricCard label="Elite candidates" value={overview.elite_candidates ?? 0} tone={overview.elite_candidates ? "success" : "neutral"} />
          <MetricCard label="Candidate-linked deployments" value={overview.candidate_linked_deployments ?? 0} tone={overview.candidate_linked_deployments ? "success" : "neutral"} />
        </div>
        <div className="researchSourceLine">
          <span>{String(data.campaign?.status ?? "unknown")}</span>
          <span>{data.source?.candidate_grain}</span>
          <span>Simulation only</span>
        </div>
      </ResearchSection>

      <ResearchSection id="funnel" title="Candidate Funnel" eyebrow="Candidate-level lifecycle" icon={<Layers3 size={17} />}>
        <div className="candidateFunnel">
          {data.candidate_funnel.map((stage, index) => (
            <div className={`funnelStage ${stage.key === "rejected" ? "failed" : ""}`} key={stage.key}>
              <div><span>{stage.label}</span><strong>{stage.count}</strong><small>{formatPercent(stage.rate_from_generated)} of generated</small></div>
              {index < data.candidate_funnel.length - 1 ? <span className="funnelConnector" title={`Rate from ${data.candidate_funnel[index + 1]?.conversion_basis ?? "prior stage"}`}>{data.candidate_funnel[index + 1]?.conversion_from_previous === null ? "" : formatPercent(data.candidate_funnel[index + 1]?.conversion_from_previous)}</span> : null}
            </div>
          ))}
        </div>
      </ResearchSection>

      <ResearchSection id="rejections" title="Rejection Analysis" eyebrow={`${data.rejection_analysis?.rejected_validation_runs ?? 0} rejected validation runs`} icon={<AlertTriangle size={17} />}>
        <div className="researchInsightGrid">
          <Distribution title="Validation rules" rows={data.rejection_analysis?.validation_rules} />
          <Distribution title="Strategy families" rows={data.rejection_analysis?.strategy_families} />
          <Distribution title="Assets" rows={data.rejection_analysis?.assets} />
          <Distribution title="Timeframes" rows={data.rejection_analysis?.timeframes} />
          <Distribution title="Market regimes" rows={data.rejection_analysis?.market_regimes} />
        </div>
        <details className="researchDetails">
          <summary>Parameter and metric failure ranges</summary>
          <div className="researchSplitTables">
            <DataTable columns={["Parameter", "Value", "Rejected runs"]} rows={(data.rejection_analysis?.parameter_ranges ?? []).map((row: any) => [row.parameter, row.value, row.rejected_runs])} />
            <DataTable columns={["Metric", "Range", "Rejected runs"]} rows={(data.rejection_analysis?.metric_ranges ?? []).map((row: any) => [row.metric, row.range, row.rejected_runs])} />
          </div>
        </details>
      </ResearchSection>

      <ResearchSection id="near-pass" title="Near-Pass Candidates" eyebrow="Existing thresholds unchanged" icon={<Target size={17} />}>
        {data.near_pass_candidates.length ? (
          <DataTable
            columns={["Candidate", "Market", "Family", "Failed gates", "Strongest", "Weakest", "Distance", "Recommendation"]}
            rows={data.near_pass_candidates.map((row) => [
              row.candidate_id,
              `${row.asset} / ${row.timeframe}`,
              row.strategy_family,
              row.failed_gates.map((gate: any) => label(gate.name)).join(", "),
              metricEvidence(row.strongest_metric),
              metricEvidence(row.weakest_metric),
              formatPercent(row.mean_distance),
              <span className={`status ${row.further_testing_justified ? "watchlist" : "avoid"}`} key={`${row.candidate_id}-recommendation`}>{row.recommendation}</span>,
            ])}
          />
        ) : <EmptyState title="No candidates are close enough to qualify" body="No tested candidate is within the conservative near-pass distance of the stored gates." />}
      </ResearchSection>

      <ResearchSection id="strategy" title="Strategy Intelligence" eyebrow={highlightLabel(data.strategy_intelligence)} icon={<FlaskConical size={17} />}>
        <IntelligenceTable rows={data.strategy_intelligence.rows} includeBest />
      </ResearchSection>

      <ResearchSection id="markets" title="Asset and Timeframe Intelligence" eyebrow="Campaign validation markets" icon={<BarChart3 size={17} />}>
        <div className="researchSplitTables">
          <div><h3>Assets</h3><IntelligenceTable rows={data.asset_intelligence.rows} /></div>
          <div><h3>Timeframes</h3><IntelligenceTable rows={data.timeframe_intelligence.rows} /></div>
        </div>
      </ResearchSection>

      <ResearchSection id="regimes" title="Regime Analysis" eyebrow="Stored market-regime buckets" icon={<Activity size={17} />}>
        <DataTable
          columns={["Regime", "Evidence", "Trades", "Profit factor", "Expectancy", "Drawdown", "Win rate", "Candidate pass rate", "Dominant failure"]}
          rows={data.regime_analysis.map((row) => [row.regime, row.evidence_available ? "Observed" : "No stored evidence", row.trades, row.evidence_available ? formatNumber(row.profit_factor) : "N/A", row.evidence_available ? formatNumber(row.expectancy) : "N/A", row.evidence_available ? formatPercent(row.drawdown) : "N/A", row.evidence_available ? formatPercent(row.win_rate) : "N/A", row.evidence_available ? formatPercent(row.candidate_pass_rate) : "N/A", label(row.dominant_failure_reason)])}
        />
      </ResearchSection>

      <ResearchSection id="history" title="Experiment History" eyebrow="One row per candidate; validation runs remain distinct" icon={<Beaker size={17} />}>
        <div className="duplicateStrip">
          <MetricCard label="Unique candidates" value={duplicates.unique_candidates ?? 0} />
          <MetricCard label="Exact duplicates" value={duplicates.exact_duplicates ?? 0} tone={duplicates.exact_duplicates ? "error" : "success"} />
          <MetricCard label="Near duplicates" value={duplicates.near_duplicates ?? 0} tone={duplicates.near_duplicates ? "warning" : "success"} />
          <MetricCard label="Duplicate outcomes" value={duplicates.duplicate_validation_outcomes ?? 0} />
          <MetricCard label="Redundant regions" value={(duplicates.redundant_parameter_regions ?? []).length} tone={(duplicates.redundant_parameter_regions ?? []).length ? "warning" : "success"} />
        </div>
        <details className="researchDetails">
          <summary>Grouped campaign experiments</summary>
          <DataTable
            columns={["Experiment", "Candidate", "Family", "Assets", "Timeframes", "Parameter version", "Result", "Runs", "Failure reasons", "Created"]}
            rows={data.experiment_history.map((row) => [
              row.experiment_id,
              row.candidate_id,
              row.strategy_family,
              row.assets.join(", "),
              row.timeframes.join(", "),
              row.parameter_version,
              label(row.result),
              row.distinct_validation_runs,
              row.failure_reasons.join(", ") || "None",
              formatDate(row.created_at),
            ])}
          />
        </details>
      </ResearchSection>

      <ResearchSection id="recommendations" title="Research Recommendations" eyebrow="Deterministic evidence rules" icon={<Target size={17} />}>
        {data.recommendations.length ? <div className="recommendationList">{data.recommendations.map((row, index) => (
          <article key={`${row.evidence_source}-${index}`}>
            <div><strong>{row.recommendation}</strong><span>{row.evidence_source}</span></div>
            <dl><dt>Evidence</dt><dd>{row.candidate_count} candidates</dd><dt>Confidence</dt><dd>{formatPercent(row.confidence)}</dd><dt>Expected benefit</dt><dd>{row.expected_benefit}</dd><dt>Falsification</dt><dd>{row.falsification_test}</dd><dt>Campaign</dt><dd>{row.campaign_version}</dd></dl>
          </article>
        ))}</div> : <EmptyState title="No recommendation met the evidence minimum" body="More completed campaign validations are required." />}
      </ResearchSection>

      <ResearchSection id="proposal" title="Next Campaign Proposal" eyebrow={proposal?.proposal_version ?? "No proposal"} icon={<Copy size={17} />}>
        {proposal ? <>
          <div className="proposalStatus"><span className="status watchlist">Review required</span><strong>Not launched</strong><small>Validation thresholds unchanged</small></div>
          <div className="proposalGrid">
            <ProposalList title="Retain families" values={proposal.strategy_families_to_retain} />
            <ProposalList title="Deprioritize families" values={proposal.strategy_families_to_deprioritize} />
            <ProposalList title="Retain assets" values={proposal.assets_to_retain} />
            <ProposalList title="Deprioritize assets" values={proposal.assets_to_deprioritize} />
            <ProposalList title="Retain timeframes" values={proposal.timeframes_to_retain} />
            <ProposalList title="Deprioritize timeframes" values={proposal.timeframes_to_deprioritize} />
          </div>
          <div className="proposalFooter"><span>Candidate count <strong>{proposal.candidate_count}</strong></span><span>Expected duplicate reduction <strong>{formatPercent(proposal.expected_duplicate_work_reduction)}</strong></span><span>Hypothesis tests <strong>{proposal.new_hypothesis_tests.length}</strong></span></div>
          <details className="researchDetails"><summary>Falsifiable hypothesis tests</summary><ol className="hypothesisList">{proposal.new_hypothesis_tests.map((value: string, index: number) => <li key={`${value}-${index}`}>{value}</li>)}</ol></details>
        </> : <EmptyState title="No campaign proposal available" body="Select a campaign with stored validation evidence." />}
      </ResearchSection>

      <details className="historicalResearch">
        <summary>Historical Research <span>Separated from campaign evidence</span></summary>
        <div className="duplicateStrip">
          <MetricCard label="Legacy validation runs" value={data.historical_research?.alpha_validation_run_count ?? 0} />
          <MetricCard label="Legacy experiments" value={data.historical_research?.strategy_experiment_count ?? 0} />
        </div>
        <DataTable
          columns={["Type", "ID", "Name", "Result", "Created"]}
          rows={[
            ...(data.historical_research?.alpha_validation_runs ?? []).map((row: any) => ["Alpha validation", row.id, `${row.candidate_count} candidates`, "Historical", formatDate(row.created_at)]),
            ...(data.historical_research?.strategy_experiments ?? []).map((row: any) => ["Strategy experiment", row.id, row.name, row.recommendation, formatDate(row.created_at)]),
          ]}
        />
      </details>

      <details className="historicalResearch">
        <summary>Research terminology</summary>
        <dl className="terminologyGrid">{Object.entries(data.terminology ?? {}).map(([term, definition]) => <div key={term}><dt>{label(term)}</dt><dd>{definition}</dd></div>)}</dl>
      </details>
    </div>
  );
}

function ResearchSection({ id, title, eyebrow, icon, children }: { id: string; title: string; eyebrow: string; icon: React.ReactNode; children: React.ReactNode }) {
  return <section className="panel researchSection" id={id}><div className="panelHeader"><div><span className="sectionLabel">{eyebrow}</span><h2>{icon}{title}</h2></div></div>{children}</section>;
}

function FilterSelect({ label: filterLabel, value, onChange, options }: { label: string; value?: string; onChange: (value: string) => void; options: Array<{ value: string; label: string }> }) {
  return <label><span>{filterLabel}</span><select value={value ?? ""} onChange={(event) => onChange(event.target.value)}><option value="">All</option>{options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>;
}

function Distribution({ title, rows = [] }: { title: string; rows?: Array<Record<string, any>> }) {
  const max = Math.max(1, ...rows.map((row) => Number(row.count ?? 0)));
  return <div className="researchDistribution"><h3>{title}</h3>{rows.length ? rows.slice(0, 8).map((row) => <div key={row.name}><span>{label(row.name)}</span><div><i style={{ width: `${Math.max(3, Number(row.count) / max * 100)}%` }} /></div><strong>{formatPercent(row.rate)}</strong><small>{row.count}</small></div>) : <small>No observed failures</small>}</div>;
}

function IntelligenceTable({ rows, includeBest = false }: { rows: Array<Record<string, any>>; includeBest?: boolean }) {
  return <DataTable
    columns={["Name", "Candidates", "Runs", "Rejection", "Profit factor", "Expectancy", "Median trades", "Median drawdown", "Stability", "CI pass", "Quality", ...(includeBest ? ["Best asset", "Best timeframe"] : []), "Dominant failure"]}
    rows={rows.map((row) => [
      row.name,
      row.candidates_tested,
      row.validation_runs,
      formatPercent(row.rejection_rate),
      formatNumber(row.average_profit_factor),
      formatNumber(row.average_expectancy),
      formatNumber(row.median_trade_count),
      formatPercent(row.median_drawdown),
      row.stability_pass_rate === null ? "Not measured" : formatPercent(row.stability_pass_rate),
      row.confidence_interval_pass_rate === null ? "Not measured" : formatPercent(row.confidence_interval_pass_rate),
      formatNumber(row.candidate_quality_score),
      ...(includeBest ? [row.best_asset ?? "None", row.best_timeframe ?? "None"] : []),
      label(row.dominant_failure_reason),
    ])}
  />;
}

function ProposalList({ title, values = [] }: { title: string; values?: string[] }) {
  return <div><span>{title}</span><strong>{values.length ? values.join(", ") : "None"}</strong></div>;
}

function highlightLabel(intelligence: ResearchCommandCenter["strategy_intelligence"]) {
  const best = intelligence.highlights?.most_promising;
  if (best) return `Most promising by validation pass evidence: ${best}`;
  const observed = intelligence.highlights?.highest_observed_quality;
  return observed ? `Highest observed quality: ${observed} (no validation pass)` : "No tested strategy families";
}

function metricEvidence(metric: Record<string, any> | undefined) {
  if (!metric?.name) return "Not measured";
  const actual = metric.actual === null || metric.actual === undefined ? "n/a" : formatNumber(metric.actual);
  const threshold = typeof metric.threshold === "number" ? formatNumber(metric.threshold) : String(metric.threshold ?? "stored rule");
  return `${label(metric.name)}: ${actual} ${metric.comparator ?? ""} ${threshold}`;
}

function stringOptions(values: string[] | undefined) {
  return (values ?? []).map((value) => ({ value, label: label(value) }));
}

function label(value: unknown) {
  if (value === null || value === undefined || value === "") return "None";
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatNumber(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString(undefined, { maximumFractionDigits: 3 }) : "N/A";
}

function formatPercent(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(number * 100 < 10 ? 1 : 0)}%` : "N/A";
}

function formatDate(value: unknown) {
  if (!value) return "N/A";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}
