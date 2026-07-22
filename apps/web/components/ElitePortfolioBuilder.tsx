"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  BarChart3,
  Check,
  CircleGauge,
  GitBranch,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  Play,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles
} from "lucide-react";
import {
  activateElitePortfolio,
  approveElitePortfolio,
  backfillElitePortfolioEvidence,
  createElitePortfolio,
  getElitePortfolioOptions,
  previewElitePortfolio,
  type ElitePortfolioConfiguration,
  type ElitePortfolioOptions,
  type ElitePortfolioResult
} from "@/lib/api";

type Phase = "configure" | "preview" | "saved" | "approved" | "activated";

const steps = [
  ["01", "Scope"],
  ["02", "Constraints"],
  ["03", "Review"],
  ["04", "Activate"]
] as const;

export function ElitePortfolioBuilder() {
  const [options, setOptions] = useState<ElitePortfolioOptions | null>(null);
  const [configuration, setConfiguration] = useState<ElitePortfolioConfiguration | null>(null);
  const [result, setResult] = useState<ElitePortfolioResult | null>(null);
  const [phase, setPhase] = useState<Phase>("configure");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    getElitePortfolioOptions()
      .then((next) => {
        if (!mounted) return;
        setOptions(next);
        setConfiguration({
          universe: [],
          families: [],
          directions: [...next.directions],
          timeframes: [...next.timeframes],
          thresholds: { ...next.default_thresholds },
          constraints: { ...next.default_constraints },
          objective: "balanced",
          custom_size: null
        });
      })
      .catch((reason) => { if (mounted) setError(message(reason)); });
    return () => { mounted = false; };
  }, []);

  const snapshotHash = snapshotFor(result);
  const analytics = (result?.analytics ?? result?.portfolio_analytics ?? {}) as Record<string, any>;
  const members = result?.members ?? (result?.selected ?? []).map((candidateKey, index) => ({ id: index, candidate_id: candidateKey }));
  const activeStep = phase === "configure" ? 0 : phase === "preview" ? 2 : phase === "saved" ? 2 : 3;
  const twoTimeframeWarning = configuration?.timeframes.length === 2
    ? "With exactly two timeframes, the exact 50% cap requires an even-sized portfolio split equally between them."
    : null;

  async function run(action: string, operation: () => Promise<ElitePortfolioResult>, nextPhase: Phase) {
    setBusy(action);
    setError(null);
    try {
      const next = await operation();
      setResult(next);
      setPhase(nextPhase);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }

  async function buildMissingEvidence() {
    setBusy("evidence");
    setError(null);
    try {
      const evidence = await backfillElitePortfolioEvidence(20);
      if (evidence.failures.length) {
        throw new Error(`Correlation evidence failed for ${evidence.failures.length} research job(s).`);
      }
      const next = await previewElitePortfolio(configuration!);
      setResult(next);
      setPhase("preview");
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }

  if (!options || !configuration) {
    return (
      <section className="eliteBuilderLoading">
        <LoaderCircle className="spin" size={24} />
        <div><span className="eyebrow">Elite Portfolio Builder</span><h1>Reading immutable elite evidence…</h1><p>{error ?? "Loading portfolio constraints and candidate options."}</p></div>
      </section>
    );
  }

  return (
    <div className="eliteBuilder">
      <header className="eliteBuilderHero">
        <div>
          <span className="eyebrow">Diversified research portfolios</span>
          <h1>Build the strongest feasible set.<br /><em>Never weaken the evidence.</em></h1>
          <p>Combine elite strategy-market variants under immutable quality, diversity, correlation, and safety constraints. An infeasible result is evidence—not an error.</p>
        </div>
        <div className="eliteSolverSeal">
          <GitBranch size={22} />
          <span>Solver version</span>
          <strong>{options.solver_version}</strong>
          <small>Deterministic · no random seed · zero automatic relaxation</small>
        </div>
      </header>

      <nav className="eliteSteps" aria-label="Portfolio construction progress">
        {steps.map(([number, label], index) => <div key={number} className={index <= activeStep ? "active" : ""}><span>{number}</span><strong>{label}</strong></div>)}
      </nav>

      {error ? <div className="eliteNotice error"><AlertTriangle size={17} /><span><strong>Action stopped</strong>{error}</span></div> : null}
      {twoTimeframeWarning ? <div className="eliteNotice"><AlertTriangle size={17} /><span><strong>Two-timeframe arithmetic</strong>{twoTimeframeWarning}</span></div> : null}

      <div className="eliteBuilderGrid">
        <main>
          <section className="elitePanel">
            <header><div><span className="eyebrow">01 · Research scope</span><h2>Choose the evidence pool</h2></div><Layers3 size={20} /></header>
            <div className="eliteChoiceSection">
              <label>Direction</label>
              <div className="eliteChoiceGrid compact">
                {options.directions.map((value) => <Choice key={value} value={value} selected={configuration.directions.includes(value)} detail={value === "short" ? "Internal simulation only" : "External observe eligible after approval"} onClick={() => setConfiguration({ ...configuration, directions: toggle(configuration.directions, value) })} />)}
              </div>
            </div>
            <div className="eliteChoiceSection">
              <label>Timeframes</label>
              <div className="elitePills">{options.timeframes.map((value) => <button key={value} className={configuration.timeframes.includes(value) ? "active" : ""} onClick={() => setConfiguration({ ...configuration, timeframes: toggle(configuration.timeframes, value) })}>{value}</button>)}</div>
            </div>
            <div className="eliteChoiceSection">
              <label>Families <small>None selected means every family</small></label>
              <div className="elitePills wrap">{options.families.map((value) => <button key={value} className={configuration.families.includes(value) ? "active" : ""} onClick={() => setConfiguration({ ...configuration, families: toggle(configuration.families, value) })}>{value}</button>)}</div>
            </div>
          </section>

          <section className="elitePanel">
            <header><div><span className="eyebrow">02 · Quality and health</span><h2>Keep the promotion gates intact</h2></div><ShieldCheck size={20} /></header>
            <div className="eliteMetricInputs">
              <NumberField label="Minimum PF" value={configuration.thresholds.minimum_profit_factor} step="0.05" onChange={(value) => threshold("minimum_profit_factor", value)} />
              <NumberField label="Minimum trades" value={configuration.thresholds.minimum_trade_count} step="1" onChange={(value) => threshold("minimum_trade_count", value)} />
              <NumberField label="Maximum drawdown" value={configuration.thresholds.maximum_drawdown} step="0.01" onChange={(value) => threshold("maximum_drawdown", value)} />
              <NumberField label="Minimum stability" value={configuration.thresholds.minimum_stability} step="0.05" onChange={(value) => threshold("minimum_stability", value)} />
              <NumberField label="Passing assets" value={configuration.thresholds.minimum_assets_passed} step="1" onChange={(value) => threshold("minimum_assets_passed", value)} />
              <NumberField label="Maximum size" value={configuration.constraints.maximum_portfolio_size} step="1" max="20" onChange={(value) => constraint("maximum_portfolio_size", value)} />
            </div>
            <p className="elitePolicyLine"><LockKeyhole size={15} /> Infeasibility never changes these values. Every excluded candidate and binding constraint is preserved.</p>
          </section>

          <section className="elitePanel">
            <header><div><span className="eyebrow">03 · Diversity and objective</span><h2>Define portfolio shape</h2></div><SlidersHorizontal size={20} /></header>
            <div className="eliteObjectiveGrid">
              {options.objectives.map((objective) => <button key={objective} className={configuration.objective === objective ? "active" : ""} onClick={() => setConfiguration({ ...configuration, objective })}><CircleGauge size={17} /><span><strong>{title(objective)}</strong><small>{objectiveDetail(objective)}</small></span></button>)}
            </div>
            <div className="eliteConstraintLedger">
              <Constraint label="Unique assets" value={`≥ ${configuration.constraints.minimum_unique_assets}`} />
              <Constraint label="Families" value={`≥ ${configuration.constraints.minimum_families}`} />
              <Constraint label="Per symbol" value={`≤ ${configuration.constraints.maximum_per_symbol}`} />
              <Constraint label="Per family" value={`≤ ${configuration.constraints.maximum_per_family}`} />
              <Constraint label="Strategy correlation" value={`≤ ${configuration.constraints.maximum_strategy_return_correlation}`} />
              <Constraint label="Timeframe share" value="2 × count ≤ total" />
            </div>
          </section>

          {result ? <PortfolioReview result={result} analytics={analytics} members={members} /> : null}
        </main>

        <aside className="eliteBuilderRail">
          <section>
            <span className="sectionLabel">Construction summary</span>
            <Metric label="Candidate variants" value={options.candidate_count} />
            <Metric label="Eligible" value={result?.eligible_count ?? "—"} />
            <Metric label="Maximum feasible" value={result?.maximum_feasible_size ?? (members.length || "—")} />
            <Metric label="Constraints relaxed" value={result?.constraint_relaxation_count ?? 0} tone="safe" />
          </section>
          <section className="eliteRailSafety">
            <ShieldCheck size={19} />
            <div><strong>Broker submission unchanged</strong><p>Construction and activation are internal. Shorts are structurally excluded from every external path.</p></div>
          </section>
          {snapshotHash ? <section><span className="sectionLabel">Immutable decision</span><code>{snapshotHash}</code><small>{result?.solver_version ?? options.solver_version}</small></section> : null}
          <section className="eliteRailActions">
            {hasInsufficientCorrelation(result) ? <button className="button secondary" disabled={Boolean(busy)} onClick={buildMissingEvidence}><RefreshCw className={busy === "evidence" ? "spin" : ""} size={16} />{busy === "evidence" ? "Building evidence..." : "Build missing correlation evidence"}</button> : null}
            <button className="button" disabled={Boolean(busy)} onClick={() => run("preview", () => previewElitePortfolio(configuration), "preview")}><Sparkles size={16} />{busy === "preview" ? "Constructing…" : "Preview portfolio"}</button>
            {phase === "preview" && result?.status === "review_ready" ? <button className="button secondary" disabled={Boolean(busy)} onClick={() => run("save", () => createElitePortfolio(configuration), "saved")}><ArrowRight size={16} />Save immutable run</button> : null}
            {phase === "saved" && result?.id && snapshotHash ? <button className="button secondary" disabled={Boolean(busy)} onClick={() => run("approve", () => approveElitePortfolio(result.id!, snapshotHash), "approved")}><Check size={16} />Approve snapshot</button> : null}
            {phase === "approved" && result?.id && snapshotHash ? <button className="button secondary" disabled={Boolean(busy)} onClick={() => run("activate", () => activateElitePortfolio(result.id!, snapshotHash, `elite-builder-${result.id}-${snapshotHash.slice(0, 12)}`), "activated")}><Play size={16} />Activate internally</button> : null}
            {result ? <button className="eliteTextButton" disabled={Boolean(busy)} onClick={() => run("refresh", () => previewElitePortfolio(configuration), "preview")}><RefreshCw size={14} />Recalculate from current evidence</button> : null}
          </section>
        </aside>
      </div>
    </div>
  );

  function threshold(key: string, value: number) {
    setConfiguration({ ...configuration!, thresholds: { ...configuration!.thresholds, [key]: value } });
  }

  function constraint(key: string, value: number) {
    setConfiguration({ ...configuration!, constraints: { ...configuration!.constraints, [key]: value } });
  }
}

function PortfolioReview({ result, analytics, members }: { result: ElitePortfolioResult; analytics: Record<string, any>; members: Array<Record<string, any>> }) {
  const infeasible = result.status === "infeasible";
  const distributions = [
    ["Direction", analytics.direction_distribution],
    ["Timeframe", analytics.timeframe_distribution],
    ["Family", analytics.family_distribution]
  ] as const;
  return (
    <section className={`elitePanel eliteReview ${infeasible ? "infeasible" : ""}`}>
      <header><div><span className="eyebrow">Construction result</span><h2>{infeasible ? "No portfolio satisfies every constraint" : `${result.maximum_feasible_size ?? members.length} members ready for review`}</h2></div>{infeasible ? <AlertTriangle size={21} /> : <BarChart3 size={21} />}</header>
      <p className="eliteReviewLead">{infeasible ? "The constructor relaxed zero constraints. Review the ranked binding constraints and deliberately change evidence requirements only if your research policy changes." : "The largest feasible portfolio found by the bounded deterministic constructor. Approval remains tied to this exact evidence snapshot."}</p>
      <div className="eliteAnalyticsStrip">
        <Metric label="Portfolio PF" value={number(analytics.portfolio_profit_factor)} />
        <Metric label="Expectancy" value={number(analytics.portfolio_expectancy)} />
        <Metric label="Max correlation" value={number(analytics.maximum_pairwise_correlation)} />
        <Metric label="Gross units" value={analytics.gross_exposure_units ?? 0} />
      </div>
      <div className="eliteDistributionGrid">
        {distributions.map(([label, distribution]) => <Distribution key={label} label={label} values={distribution ?? {}} />)}
      </div>
      {result.binding_constraints?.length ? <div className="eliteBinding"><h3>Binding constraints</h3>{result.binding_constraints.slice(0, 8).map((row) => <div key={row.constraint}><span>{title(row.constraint)}</span><strong>{row.excluded_candidates_or_pairs}</strong></div>)}</div> : null}
      {members.length ? <div className="eliteMembers"><h3>Selected members</h3>{members.map((row, index) => <article key={`${row.candidate_id}-${row.symbol ?? "variant"}-${row.timeframe ?? "all"}`}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{row.symbol ? `${row.symbol} · ${row.timeframe}` : row.candidate_id}</strong><small>{row.strategy_family ?? row.strategy_direction ?? "Selected strategy variant"}</small></div><em>{title(row.execution_capability ?? "selected")}</em></article>)}</div> : null}
    </section>
  );
}

function Choice({ value, selected, detail, onClick }: { value: string; selected: boolean; detail: string; onClick: () => void }) {
  return <button className={selected ? "active" : ""} onClick={onClick}><span>{selected ? <Check size={14} /> : null}</span><div><strong>{title(value)}</strong><small>{detail}</small></div></button>;
}

function NumberField({ label, value, onChange, step, max }: { label: string; value: unknown; onChange: (value: number) => void; step: string; max?: string }) {
  return <label><span>{label}</span><input type="number" value={Number(value ?? 0)} step={step} max={max} onChange={(event) => onChange(Number(event.target.value))} /></label>;
}

function Constraint({ label, value }: { label: string; value: unknown }) { return <div><span>{label}</span><strong>{String(value)}</strong></div>; }
function Metric({ label, value, tone }: { label: string; value: unknown; tone?: string }) { return <div className={`eliteMetric ${tone ?? ""}`}><span>{label}</span><strong>{String(value)}</strong></div>; }
function Distribution({ label, values }: { label: string; values: Record<string, number> }) {
  const total = Object.values(values).reduce((sum, value) => sum + Number(value), 0);
  return <div className="eliteDistribution"><h3>{label}</h3>{Object.entries(values).map(([key, value]) => <div key={key}><span>{title(key)}</span><i><b style={{ width: `${total ? (Number(value) / total) * 100 : 0}%` }} /></i><strong>{value}</strong></div>)}</div>;
}

function toggle(values: string[], value: string) { return values.includes(value) ? values.filter((item) => item !== value) : [...values, value].sort(); }
function title(value: string) { return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function number(value: unknown) { return value == null ? "—" : Number(value).toFixed(3); }
function message(reason: unknown) { return reason instanceof Error ? reason.message : "The portfolio operation failed."; }
function snapshotFor(result: ElitePortfolioResult | null) { return result?.snapshot?.decision_hash ?? result?.snapshot?.snapshot_hash ?? result?.snapshot_hash ?? null; }
function hasInsufficientCorrelation(result: ElitePortfolioResult | null) {
  return Boolean(result?.binding_constraints?.some((row) => row.constraint === "SIGNAL_CORRELATION_INSUFFICIENT" || row.constraint === "STRATEGY_RETURN_CORRELATION_INSUFFICIENT"));
}
function objectiveDetail(value: string) {
  if (value === "profit_factor") return "Prioritize payoff quality";
  if (value === "expectancy") return "Prioritize expected return";
  if (value === "minimum_drawdown") return "Prioritize capital defense";
  return "Balance quality and diversity";
}
