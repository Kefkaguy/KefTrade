"use client";

import Link from "next/link";
import { ArrowUpDown, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getResearchIntelligence, type ResearchIntelligence } from "@/lib/api";
import { number } from "@/lib/format";

type SortKey = "global_rank" | "research_score" | "review_priority_score" | "trade_count" | "profit_factor";

export function ResearchIntelligenceDashboard() {
  const [data, setData] = useState<ResearchIntelligence | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [sortKey, setSortKey] = useState<SortKey>("global_rank");
  const [filters, setFilters] = useState({ asset: "", assetClass: "", strategy: "", timeframe: "", classification: "", deployment: "", setup: "", freshness: "" });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [compareIds, setCompareIds] = useState<string[]>([]);

  useEffect(() => {
    getResearchIntelligence()
      .then((payload) => {
        setData(payload);
        setSelectedId(String(payload.rankings[0]?.candidate_id ?? ""));
        setCompareIds(payload.rankings.slice(0, 4).map((row) => String(row.candidate_id)));
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Research Intelligence unavailable"))
      .finally(() => setLoading(false));
  }, []);

  const rankings = data?.rankings ?? [];
  const filtered = useMemo(() => rankings.filter((row) => matchesFilters(row, filters)).sort((a, b) => sortRows(a, b, sortKey)), [rankings, filters, sortKey]);
  const selected = rankings.find((row) => String(row.candidate_id) === selectedId) ?? filtered[0];
  const compareRows = rankings.filter((row) => compareIds.includes(String(row.candidate_id))).slice(0, 4);

  if (loading) {
    return (
      <div className="pageStack">
        <PageTitle title="Research Intelligence" description="Loading stored evidence rankings." />
        <Card title="Loading ranking engine" eyebrow="Stored evidence"><EmptyState title="Loading Research Intelligence." body="Composite scores, rankings, leaderboards, and portfolio context are loading." /></Card>
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="pageStack">
        <PageTitle title="Research Intelligence" description="Deterministic research rankings from stored evidence." />
        <Card title="Research Intelligence unavailable" eyebrow="Subsystem error"><EmptyState title="Unable to load rankings." body={error ?? "Unknown error."} /></Card>
      </div>
    );
  }

  return (
    <div className="pageStack">
      <PageTitle
        title="Research Intelligence"
        description="Deterministic research quality and review-priority rankings from stored evidence only."
        actions={<Link className="button" href="/mission-control">Mission Control</Link>}
      />

      <section className="paperHero">
        <div>
          <span className="eyebrow">RESEARCH ONLY</span>
          <h2>{String(data.summary.top_ranked_asset ?? "No ranked asset")}</h2>
          <p>Research rankings are based on historical and stored evidence. They are not trading recommendations.</p>
        </div>
        <div className="safetyStack">
          <span>Simulation only</span>
          <span>No broker routing</span>
          <span>No live execution</span>
        </div>
      </section>

      <Card title="Research Intelligence Summary" eyebrow="Composite evidence">
        <div className="metricGrid">
          <MetricCard label="Candidates ranked" value={data.summary.candidates_ranked ?? 0} />
          <MetricCard label="High-quality evidence" value={data.summary.high_quality_evidence_count ?? 0} />
          <MetricCard label="Strong candidates" value={data.summary.strong_candidate_count ?? 0} />
          <MetricCard label="Incomplete evidence" value={data.summary.incomplete_evidence_count ?? 0} tone="warning" />
          <MetricCard label="Weak/rejected" value={data.summary.rejected_or_weak_count ?? 0} />
          <MetricCard label="Active setups" value={data.summary.active_setup_count ?? 0} />
          <MetricCard label="Stale candidates" value={data.summary.stale_candidate_count ?? 0} tone={(data.summary.stale_candidate_count ?? 0) ? "warning" : "success"} />
          <MetricCard label="Average score" value={number(data.summary.average_research_score ?? 0, 2)} />
          <MetricCard label="Top asset" value={String(data.summary.top_ranked_asset ?? "n/a")} />
          <MetricCard label="Top strategy" value={String(data.summary.top_ranked_strategy ?? "n/a")} />
        </div>
      </Card>

      {data.subsystem_errors.length ? (
        <Card title="Partial results" eyebrow="Subsystem errors">
          <div className="warningList">{data.subsystem_errors.map((item) => <span key={`${item.subsystem}-${item.error}`}>{item.subsystem}: {item.error}</span>)}</div>
        </Card>
      ) : null}

      <Card title="Opportunity Ranking Table" eyebrow="Research quality rank and review priority">
        <FilterBar rankings={rankings} filters={filters} onFilters={setFilters} sortKey={sortKey} onSortKey={setSortKey} />
        {filtered.length ? (
          <DataTable
            columns={["Rank", "Candidate", "Score", "Classification", "Verdict", "Priority", "Freshness", "Health", "PF", "Expectancy", "Trades", "Drawdown", "OOS", "Stability", "Setup", "Blocks", "Links"]}
            rows={filtered.map((row) => [
              row.global_rank,
              <button key="candidate" className="tableLink" type="button" onClick={() => setSelectedId(String(row.candidate_id))}>{row.symbol} <small>{row.timeframe} / {row.strategy}</small></button>,
              number(row.research_score, 2),
              row.classification,
              row.current_verdict,
              row.review_priority,
              row.data_freshness,
              row.deployment_health,
              formatMetric(row.metrics?.profit_factor),
              formatMetric(row.metrics?.expectancy),
              formatMetric(row.metrics?.trade_count),
              formatMetric(row.metrics?.max_drawdown),
              formatMetric(row.oos_score),
              formatMetric(row.stability),
              row.latest_setup_state,
              Array.isArray(row.blocking_issues) && row.blocking_issues.length ? row.blocking_issues.join("; ") : "none",
              <span key="links" className="inlineLinks"><Link href={String(row.links?.candidate_detail ?? "/promising")}>Candidate</Link><Link href={String(row.links?.validation_detail ?? "/validation")}>Validation</Link><Link href="/paper#signal-review">Signal</Link><Link href="/mission-control">Deploy</Link><Link href="/paper">Paper</Link></span>
            ])}
          />
        ) : <EmptyState title="No ranked candidates match the filters." body="Adjust filters or add stored experiment and validation evidence." />}
      </Card>

      <div className="dashboardGrid wideLeft">
        <ScoreBreakdown row={selected} />
        <Card title="Research Focus" eyebrow="Evidence-based priority">
          <div className="scoreList">
            {data.review_priorities.slice(0, 8).map((row) => <span key={String(row.candidate_id)}>{String(row.candidate_id)} <strong>{String(row.review_priority)}</strong><small>{String(row.reason ?? "")}</small></span>)}
          </div>
        </Card>
      </div>

      <div className="dashboardGrid">
        <Leaderboard title="Strategy Leaderboard" rows={data.strategy_leaderboard} columns={["strategy", "tested_candidates", "average_composite_score", "median_score", "total_trade_sample", "best_performing_asset", "weakest_asset", "active_deployments"]} />
        <Leaderboard title="Asset Leaderboard" rows={data.asset_leaderboard} columns={["symbol", "strategies_tested", "strongest_strategy", "average_research_score", "highest_research_score", "current_setup_count", "deployment_count", "data_freshness"]} />
      </div>

      <Card title="Comparison Workspace" eyebrow="Two to four candidates">
        <div className="toolbar">
          {rankings.slice(0, 12).map((row) => (
            <label key={String(row.candidate_id)} className="checkRow">
              <input type="checkbox" checked={compareIds.includes(String(row.candidate_id))} onChange={() => toggleCompare(String(row.candidate_id), compareIds, setCompareIds)} />
              {String(row.candidate_id)}
            </label>
          ))}
        </div>
        {compareRows.length >= 2 ? (
          <DataTable
            columns={["Candidate", "Score", "PF", "Expectancy", "Trades", "Drawdown", "OOS", "Walk-forward", "Regime", "Cross asset", "Freshness", "Health", "Setup", "Notes"]}
            rows={compareRows.map((row) => [row.candidate_id, number(row.research_score, 2), formatMetric(row.metrics?.profit_factor), formatMetric(row.metrics?.expectancy), formatMetric(row.metrics?.trade_count), formatMetric(row.metrics?.max_drawdown), formatMetric(row.oos_score), formatMetric(row.walk_forward_stability), formatMetric(row.regime_consistency), formatMetric(row.cross_asset_consistency), row.data_freshness, row.deployment_health, row.latest_setup_state, Array.isArray(row.score?.missing_inputs) && row.score.missing_inputs.length ? "Less complete evidence" : "Stored evidence available"])}
          />
        ) : <EmptyState title="Select at least two candidates." body="The comparison highlights stored-evidence differences without declaring a trading winner." />}
      </Card>

      <Card title="Portfolio Intelligence" eyebrow="Research coverage only">
        <div className="dashboardGrid">
          <CounterList title="Asset concentration" rows={data.portfolio_intelligence.concentration_by_asset ?? []} />
          <CounterList title="Strategy concentration" rows={data.portfolio_intelligence.concentration_by_strategy ?? []} />
          <CounterList title="Asset class concentration" rows={data.portfolio_intelligence.concentration_by_asset_class ?? []} />
        </div>
        <div className="actionNote">
          <strong>Diversification score: {String(data.portfolio_intelligence.research_diversification_score?.score ?? 0)}/100</strong>
          <p>{String(data.portfolio_intelligence.diversification_methodology ?? "")}</p>
        </div>
        {(data.portfolio_intelligence.warnings ?? []).length ? <div className="warningList">{data.portfolio_intelligence.warnings.map((warning: string) => <span key={warning}>{warning}</span>)}</div> : null}
      </Card>
    </div>
  );
}

function FilterBar({ rankings, filters, onFilters, sortKey, onSortKey }: { rankings: Array<Record<string, any>>; filters: Record<string, string>; onFilters: (filters: any) => void; sortKey: SortKey; onSortKey: (key: SortKey) => void }) {
  return (
    <div className="toolbar">
      <Select label="Asset" value={filters.asset} values={unique(rankings, "symbol")} onChange={(value) => onFilters({ ...filters, asset: value })} />
      <Select label="Class" value={filters.assetClass} values={unique(rankings, "asset_class")} onChange={(value) => onFilters({ ...filters, assetClass: value })} />
      <Select label="Strategy" value={filters.strategy} values={unique(rankings, "strategy")} onChange={(value) => onFilters({ ...filters, strategy: value })} />
      <Select label="Timeframe" value={filters.timeframe} values={unique(rankings, "timeframe")} onChange={(value) => onFilters({ ...filters, timeframe: value })} />
      <Select label="Classification" value={filters.classification} values={unique(rankings, "classification")} onChange={(value) => onFilters({ ...filters, classification: value })} />
      <Select label="Deployment" value={filters.deployment} values={unique(rankings, "deployment_health")} onChange={(value) => onFilters({ ...filters, deployment: value })} />
      <Select label="Setup" value={filters.setup} values={unique(rankings, "latest_setup_state")} onChange={(value) => onFilters({ ...filters, setup: value })} />
      <Select label="Freshness" value={filters.freshness} values={unique(rankings, "data_freshness")} onChange={(value) => onFilters({ ...filters, freshness: value })} />
      <button className="button subtle" type="button" onClick={() => onSortKey(sortKey === "global_rank" ? "research_score" : "global_rank")}><ArrowUpDown size={14} /> Sort {sortKey}</button>
      <button className="button subtle" type="button" onClick={() => onFilters({ asset: "", assetClass: "", strategy: "", timeframe: "", classification: "", deployment: "", setup: "", freshness: "" })}><RefreshCw size={14} /> Reset</button>
    </div>
  );
}

function ScoreBreakdown({ row }: { row?: Record<string, any> }) {
  if (!row) return <Card title="Score Breakdown" eyebrow="Selected candidate"><EmptyState title="No candidate selected." body="Select a ranked candidate to inspect score components." /></Card>;
  const components = Object.entries(row.score?.components ?? {});
  return (
    <Card title="Score Breakdown" eyebrow={String(row.candidate_id)}>
      <div className="scoreList">
        <span>Composite score <strong>{number(row.research_score, 2)}/100</strong></span>
        <span>Classification <strong>{String(row.classification)}</strong></span>
        <span>Calculation version <strong>{String(row.score?.calculation_version)}</strong></span>
      </div>
      <DataTable columns={["Component", "Awarded", "State", "Why"]} rows={components.map(([name, component]: [string, any]) => [name.replaceAll("_", " "), `${number(component.weighted_score, 2)}/${component.weight}`, component.state, component.detail])} />
      <div className="actionNote"><strong>Explanation</strong><p>{String(row.score?.explanation ?? "")}</p></div>
    </Card>
  );
}

function Leaderboard({ title, rows, columns }: { title: string; rows: Array<Record<string, any>>; columns: string[] }) {
  return (
    <Card title={title} eyebrow="Weighted by sample where available">
      {rows.length ? <DataTable columns={columns.map((column) => column.replaceAll("_", " "))} rows={rows.slice(0, 10).map((row) => columns.map((column) => formatCell(row[column])))} /> : <EmptyState title="No leaderboard rows yet." body="Leaderboard data appears after candidates are ranked." />}
    </Card>
  );
}

function CounterList({ title, rows }: { title: string; rows: Array<{ name: string; count: number }> }) {
  return <div className="scoreList"><strong>{title}</strong>{rows.length ? rows.slice(0, 8).map((row) => <span key={row.name}>{row.name} <strong>{row.count}</strong></span>) : <span>None <strong>0</strong></span>}</div>;
}

function Select({ label, value, values, onChange }: { label: string; value: string; values: string[]; onChange: (value: string) => void }) {
  return <label className="field compactField"><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}><option value="">All</option>{values.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>;
}

function matchesFilters(row: Record<string, any>, filters: Record<string, string>) {
  return (!filters.asset || row.symbol === filters.asset)
    && (!filters.assetClass || row.asset_class === filters.assetClass)
    && (!filters.strategy || row.strategy === filters.strategy)
    && (!filters.timeframe || row.timeframe === filters.timeframe)
    && (!filters.classification || row.classification === filters.classification)
    && (!filters.deployment || row.deployment_health === filters.deployment)
    && (!filters.setup || row.latest_setup_state === filters.setup)
    && (!filters.freshness || row.data_freshness === filters.freshness);
}

function sortRows(a: Record<string, any>, b: Record<string, any>, key: SortKey) {
  if (key === "global_rank") return Number(a.global_rank ?? 9999) - Number(b.global_rank ?? 9999);
  if (key === "trade_count") return Number(b.metrics?.trade_count ?? 0) - Number(a.metrics?.trade_count ?? 0);
  if (key === "profit_factor") return Number(b.metrics?.profit_factor ?? 0) - Number(a.metrics?.profit_factor ?? 0);
  return Number(b[key] ?? 0) - Number(a[key] ?? 0);
}

function unique(rows: Array<Record<string, any>>, key: string) {
  return Array.from(new Set(rows.map((row) => String(row[key] ?? "")).filter(Boolean))).sort();
}

function formatMetric(value: unknown) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? number(numeric, 4) : "Missing";
}

function formatCell(value: unknown) {
  const numeric = Number(value);
  return Number.isFinite(numeric) && value !== "" ? number(numeric, 3) : String(value ?? "n/a");
}

function toggleCompare(id: string, compareIds: string[], setCompareIds: (ids: string[]) => void) {
  if (compareIds.includes(id)) {
    setCompareIds(compareIds.filter((item) => item !== id));
    return;
  }
  if (compareIds.length >= 4) return;
  setCompareIds([...compareIds, id]);
}
