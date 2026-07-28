"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  BarChart3,
  Check,
  CircleGauge,
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
  configurationWithProfile,
  createElitePortfolio,
  createPaperLabRun,
  dedupeResearchChampions,
  getElitePortfolioRecommendation,
  getElitePortfolioRuns,
  getChampionValidationDiagnostics,
  getChampionValidationQueue,
  getElitePortfolioOptions,
  getPaperLabPreview,
  getResearchChampionStatus,
  importResearchChampions,
  PAPER_LAB_MODE,
  previewElitePortfolio,
  runChampionValidation,
  type ChampionDedupeResult,
  type ChampionValidationDiagnostics,
  type ChampionValidationQueue,
  type ChampionValidationRunResult,
  type ChampionValidationState,
  type ElitePortfolioConfiguration,
  type ElitePortfolioHardRule,
  type ElitePortfolioOptions,
  type ElitePortfolioProfile,
  type ElitePortfolioRecommendation,
  type ElitePortfolioResult,
  type ElitePortfolioRunList,
  type ResearchChampionImportResult,
  type ResearchChampionStatus
} from "@/lib/api";
import { EliteActivationWorkspace } from "@/components/EliteActivationWorkspace";

type Phase = "configure" | "preview" | "saved" | "approved" | "activated";

const steps = [
  ["01", "Import champions"],
  ["02", "Validate evidence"],
  ["03", "Build portfolio"],
  ["04", "Activate"]
] as const;

export function ElitePortfolioBuilder() {
  const [options, setOptions] = useState<ElitePortfolioOptions | null>(null);
  const [configuration, setConfiguration] = useState<ElitePortfolioConfiguration | null>(null);
  const [result, setResult] = useState<ElitePortfolioResult | null>(null);
  const [championStatus, setChampionStatus] = useState<ResearchChampionStatus | null>(null);
  const [championImport, setChampionImport] = useState<ResearchChampionImportResult | null>(null);
  const [dedupeResult, setDedupeResult] = useState<ChampionDedupeResult | null>(null);
  const [recommendation, setRecommendation] = useState<ElitePortfolioRecommendation | null>(null);
  // Step 04 must survive a page refresh, so which portfolio it opens comes from
  // the backend rather than from whatever React state the approve click left.
  // Tracked separately from the paper lab run: an approved diversified (or
  // Single Elite Test) run and an approved paper lab run can coexist, and
  // must never collapse into a single "the activatable run" slot -- that
  // would silently hide one of them.
  const [activatableRunId, setActivatableRunId] = useState<number | null>(null);
  const [paperLabActivatableRunId, setPaperLabActivatableRunId] = useState<number | null>(null);
  const [paperLabResult, setPaperLabResult] = useState<ElitePortfolioResult | null>(null);
  const [paperLabPhase, setPaperLabPhase] = useState<"idle" | "preview" | "saved">("idle");
  const [validationQueue, setValidationQueue] = useState<ChampionValidationQueue | null>(null);
  const [validationResult, setValidationResult] = useState<ChampionValidationRunResult | null>(null);
  const [validationDiagnostics, setValidationDiagnostics] = useState<ChampionValidationDiagnostics | null>(null);
  // Running totals across the batched calls that drain one queue, so a long
  // validation shows progress instead of an indefinite spinner.
  const [validationProgress, setValidationProgress] = useState<{ examined: number; validated: number; remaining: number } | null>(null);
  const stopRequested = useRef(false);
  const [phase, setPhase] = useState<Phase>("configure");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Kept out of the page-level banner: a validation problem belongs to step 2
  // and must not read as "the whole builder failed".
  const [validationError, setValidationError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);

  useEffect(() => {
    let mounted = true;
    getElitePortfolioOptions()
      .then((next) => {
        if (!mounted) return;
        setOptions(next);
        setConfiguration({
          profile: next.default_profile ?? "strict_diversified",
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

  useEffect(() => {
    let mounted = true;
    getResearchChampionStatus()
      .then((next) => { if (mounted) setChampionStatus(next); })
      .catch(() => { /* The builder can still function without the intake summary. */ });
    getChampionValidationQueue(25)
      .then((next) => { if (mounted) setValidationQueue(next); })
      .catch((reason) => { if (mounted) setValidationError(message(reason)); });
    getChampionValidationDiagnostics(25)
      .then((next) => { if (mounted) setValidationDiagnostics(next); })
      .catch(() => { /* Diagnostics are a read-only aid, never required to run validation. */ });
    refreshActivatableRuns();
    return () => { mounted = false; };

    function refreshActivatableRuns() {
      getElitePortfolioRuns(20)
        .then((next) => { if (mounted) applyRunList(next); })
        .catch(() => { /* Step 04 simply stays locked if the run list is unavailable. */ });
    }
  }, []);

  // A diversified/Single-Elite-Test run and a paper lab run are tracked in
  // separate slots on purpose: both can be approved and activatable at once,
  // and collapsing them into one "the activatable run" id would silently hide
  // whichever one is not newest.
  function applyRunList(list: ElitePortfolioRunList) {
    const diversified = list.activatable.find((row) => row.mode !== PAPER_LAB_MODE) ?? null;
    const lab = list.activatable.find((row) => row.mode === PAPER_LAB_MODE) ?? null;
    setActivatableRunId(diversified ? diversified.id : null);
    setPaperLabActivatableRunId(lab ? lab.id : null);
  }

  const snapshotHash = snapshotFor(result);
  const analytics = (result?.analytics ?? result?.portfolio_analytics ?? {}) as Record<string, any>;
  const members = result?.members ?? (result?.selected ?? []).map((candidateKey, index) => ({ id: index, candidate_id: candidateKey }));
  const solverPool = options?.candidate_count ?? 0;
  const finalElites = championStatus?.final_elites ?? solverPool;
  const pendingValidation = validationQueue?.pending_validation ?? championStatus?.pending_validation ?? 0;
  const backlog = championStatus?.eligible_promoted_jobs ?? 0;
  const workflowStep = championStatus?.research_champions ? 1 : 0;
  const activeStep = phase === "configure" ? workflowStep : phase === "preview" ? 2 : phase === "saved" ? 2 : 3;
  // Strict priority, so the seal always names the one thing that unblocks the
  // next stage rather than the most recently touched one.
  const nextAction = pendingValidation
    ? "Validate champions"
    : backlog && !championStatus?.research_champions
      ? "Import champions"
      : finalElites
        ? "Build portfolio"
        : backlog
          ? "Import champions"
          : "Expand research";
  // Only the strict profile enforces an exact half; Small Paper Launch uses a
  // two-thirds cap precisely so an odd-sized portfolio is reachable.
  const twoTimeframeWarning = configuration?.timeframes.length === 2 && (configuration?.profile ?? "strict_diversified") === "strict_diversified"
    ? "Strict Diversified caps any one timeframe at half the portfolio. With exactly two timeframes selected that forces an even-sized, evenly split portfolio. Small Paper Launch uses a two-thirds cap instead."
    : null;

  async function run(action: string, operation: () => Promise<ElitePortfolioResult>, nextPhase: Phase) {
    setBusy(action);
    setError(null);
    try {
      const next = await operation();
      setResult(next);
      setPhase(nextPhase);
      if (nextPhase === "approved" || nextPhase === "activated") {
        // Ask the backend which run is activatable rather than assuming it is
        // this one, so a refresh lands on exactly the same portfolio.
        await getElitePortfolioRuns(20)
          .then((runs) => applyRunList(runs))
          .catch(() => setActivatableRunId(next.id ?? null));
      }
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

  async function importChampions() {
    setBusy("champions");
    setError(null);
    try {
      // No batch size here: the backlog query is already bounded to eligible,
      // not-yet-imported jobs, so this always means "import all of them".
      const imported = await importResearchChampions({ minProfitFactor: 1.25, minTrades: 30, maxDrawdown: 0.12 });
      setChampionImport(imported);
      setChampionStatus(imported.status);
      const [nextOptions, nextQueue] = await Promise.all([getElitePortfolioOptions(), getChampionValidationQueue(25)]);
      setOptions(nextOptions);
      setValidationQueue(nextQueue);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }

  /**
   * Selecting a profile must replace the visible constraint values, not just
   * the profile name: the backend merges explicit `constraints` over the
   * preset, so keeping the strict defaults here silently builds a strict
   * portfolio no matter which profile is highlighted.
   */
  function applyProfile(profileId: string): ElitePortfolioConfiguration | null {
    const profile = (options?.profiles ?? []).find((row) => row.id === profileId);
    if (!profile || !configuration) return null;
    const next = configurationWithProfile(configuration, profile);
    setConfiguration(next);
    return next;
  }

  async function loadRecommendation() {
    setBusy("recommend");
    setError(null);
    try {
      setRecommendation(await getElitePortfolioRecommendation());
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }

  async function previewPaperLab() {
    setBusy("paperlab-preview");
    setError(null);
    try {
      setPaperLabResult(await getPaperLabPreview());
      setPaperLabPhase("preview");
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }

  async function savePaperLabRun() {
    setBusy("paperlab-save");
    setError(null);
    try {
      const saved = await createPaperLabRun();
      setPaperLabResult(saved);
      setPaperLabPhase("saved");
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }

  async function approvePaperLabRun() {
    if (!paperLabResult?.id) return;
    const hash = snapshotFor(paperLabResult);
    if (!hash) return;
    setBusy("paperlab-approve");
    setError(null);
    try {
      const approved = await approveElitePortfolio(paperLabResult.id, hash);
      setPaperLabResult(approved);
      await getElitePortfolioRuns(20).then((runs) => applyRunList(runs)).catch(() => setPaperLabActivatableRunId(paperLabResult.id ?? null));
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }

  async function dedupeChampions() {
    setBusy("dedupe");
    setError(null);
    try {
      const outcome = await dedupeResearchChampions();
      setDedupeResult(outcome);
      setChampionStatus(outcome.status);
      const [nextOptions, nextQueue] = await Promise.all([getElitePortfolioOptions(), getChampionValidationQueue(25)]);
      setOptions(nextOptions);
      setValidationQueue(nextQueue);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }

  async function validateChampions(revalidate: boolean) {
    setBusy(revalidate ? "revalidate" : "validate");
    setValidationError(null);
    stopRequested.current = false;
    let examined = 0;
    let validated = 0;
    try {
      // Each server call is bounded by its own wall-clock budget, so draining a
      // large queue means calling repeatedly rather than holding one request
      // open. Every champion commits its own verdict, so stopping between
      // batches — deliberately or by error — never loses completed work.
      for (;;) {
        const outcome = await runChampionValidation({ revalidate });
        examined += outcome.examined;
        validated += outcome.validated;
        setValidationResult(outcome);
        setValidationQueue(outcome.status);
        setValidationProgress({ examined, validated, remaining: outcome.remaining });

        const finished = !outcome.budget_exhausted || outcome.examined === 0 || stopRequested.current;
        if (finished) break;
      }
      // A graduation changes the solver's candidate pool, so both the champion
      // summary and the portfolio options are re-read rather than inferred.
      const [nextStatus, nextOptions, nextDiagnostics] = await Promise.all([
        getResearchChampionStatus(),
        getElitePortfolioOptions(),
        getChampionValidationDiagnostics(25)
      ]);
      setChampionStatus(nextStatus);
      setOptions(nextOptions);
      setValidationDiagnostics(nextDiagnostics);
    } catch (reason) {
      setValidationError(message(reason));
    } finally {
      stopRequested.current = false;
      setValidationProgress(null);
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
          <span className="eyebrow">Elite graduation workflow</span>
          <h1>Turn research winners into elites.<br /><em>One gate at a time.</em></h1>
          <p className="elitePrimaryLead">Promoted jobs → research champions → validated elites → portfolio. A champion only becomes an elite by surviving evidence it was never fitted to.</p>
          <p>Combine elite strategy-market variants under immutable quality, diversity, correlation, and safety constraints. An infeasible result is evidence—not an error.</p>
        </div>
        <div className="eliteSolverSeal">
          <Sparkles size={22} />
          <span>Next action</span>
          <strong>{nextAction}</strong>
          <small className="eliteActionHint">Use the highlighted step below. Portfolio solver controls stay hidden until you explicitly open the advanced builder.</small>
          <small>Deterministic · no random seed · zero automatic relaxation</small>
        </div>
      </header>

      <nav className="eliteSteps" aria-label="Portfolio construction progress">
        {steps.map(([number, label], index) => <div key={number} className={index <= activeStep ? "active" : ""}><span>{number}</span><strong>{label}</strong></div>)}
      </nav>

      {error ? <div className="eliteNotice error"><AlertTriangle size={17} /><span><strong>Action stopped</strong>{error}</span></div> : null}
      {twoTimeframeWarning ? <div className="eliteNotice"><AlertTriangle size={17} /><span><strong>Two-timeframe arithmetic</strong>{twoTimeframeWarning}</span></div> : null}

      <EliteWorkflowGuide status={championStatus} queue={validationQueue} finalCandidateCount={options.candidate_count} />

      <ResearchChampionIntake
        status={championStatus}
        result={championImport}
        dedupeResult={dedupeResult}
        busy={busy === "champions"}
        dedupeBusy={busy === "dedupe"}
        onImport={importChampions}
        onDedupe={dedupeChampions}
      />

      <EliteValidationQueue
        status={championStatus}
        queue={validationQueue}
        result={validationResult}
        diagnostics={validationDiagnostics}
        error={validationError}
        busy={busy}
        progress={validationProgress}
        onValidate={validateChampions}
        onStop={() => { stopRequested.current = true; }}
      />

      <section className="eliteAdvancedToggle">
        <div>
          <span className="eyebrow">Advanced</span>
          <h2>Portfolio builder is step 3</h2>
          <p>
            {solverPool
              ? `${solverPool.toLocaleString()} elite strategy-market variant${solverPool === 1 ? "" : "s"} are visible to the solver. Research champions do not enter it until they validate.`
              : "No final elite exists yet. Validate champions first — the solver reads final elites only."}
          </p>
        </div>
        <button type="button" className="button secondary" onClick={() => setShowAdvanced((value) => !value)}>
          {showAdvanced ? "Hide portfolio builder" : "Open portfolio builder"}
        </button>
      </section>

      {showAdvanced ? <div className="eliteBuilderGrid">
        <main>
          <PortfolioProfilePicker
            options={options}
            configuration={configuration}
            recommendation={recommendation}
            busy={busy}
            onSelect={(profile) => applyProfile(profile)}
            onRecommend={loadRecommendation}
            onApplyRecommendation={(profile) => {
              const next = applyProfile(profile);
              if (next) run("preview", () => previewElitePortfolio(next), "preview");
            }}
          />

          {result ? <PortfolioFeasibilityPanel result={result} /> : null}

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
              {/* Rendered from the active profile: the cap is 1/2 under Strict and 2/3 under Small Paper Launch, so a fixed label would misdescribe two of the three profiles. */}
              <Constraint label="Timeframe share" value={`count ≤ ${Number(configuration.constraints.timeframe_cap_numerator ?? 1)}/${Number(configuration.constraints.timeframe_cap_denominator ?? 2)} × total`} />
            </div>
            <div className="eliteHardRules">
              <h3>Hard rules (never relaxed)</h3>
              {hardRules(options).map((rule) => (
                <div key={rule.id} className="eliteHardRule">
                  <strong>{rule.label}</strong>
                  <p>{rule.description}</p>
                </div>
              ))}
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
            {result ? <button className="eliteTextButton" disabled={Boolean(busy)} onClick={() => run("refresh", () => previewElitePortfolio(configuration), "preview")}><RefreshCw size={14} />Recalculate from current evidence</button> : null}
            {/* Saving mints the immutable snapshot; approval is what Step 04
                requires, so neither can be skipped on the way to activation. */}
            {phase === "preview" && result?.status === "review_ready" ? <button className="button secondary" disabled={Boolean(busy)} onClick={() => run("save", () => createElitePortfolio(configuration), "saved")}><ArrowRight size={16} />Save immutable run</button> : null}
            {phase === "saved" && result?.id && snapshotHash ? <button className="button secondary" disabled={Boolean(busy)} onClick={() => run("approve", () => approveElitePortfolio(result.id!, snapshotHash), "approved")}><Check size={16} />Approve portfolio</button> : null}
          </section>
        </aside>
      </div> : null}

      <PaperLabPanel
        result={paperLabResult}
        phase={paperLabPhase}
        busy={busy}
        onPreview={previewPaperLab}
        onSave={savePaperLabRun}
        onApprove={approvePaperLabRun}
      />

      {/* Step 04 acts only on an approved immutable snapshot, and finds it from
          the backend so it is still there after a refresh. Diversified/Single
          Elite Test and the paper lab are two independent activatable runs --
          either, both, or neither may exist at a given time. */}
      {activatableRunId ? <EliteActivationWorkspace portfolioId={activatableRunId} /> : null}
      {paperLabActivatableRunId ? <EliteActivationWorkspace portfolioId={paperLabActivatableRunId} /> : null}
      {!activatableRunId && !paperLabActivatableRunId ? (
        <section className="eliteActivationLocked">
          <div>
            <span className="eyebrow">Step 4 · Activation</span>
            <h2>Activation unlocks once a portfolio is approved</h2>
            <p>
              Build a diversified portfolio, a Single Elite Test, or an All Validated Elites Paper Lab run in step 3,
              save it as an immutable run, then approve it. This section then loads that exact approved snapshot and
              drives internal activation, Alpaca Paper approval and execution enablement.
            </p>
          </div>
          <LockKeyhole size={20} />
        </section>
      ) : null}
    </div>
  );

  function threshold(key: string, value: number) {
    setConfiguration({ ...configuration!, thresholds: { ...configuration!.thresholds, [key]: value } });
  }

  function constraint(key: string, value: number) {
    setConfiguration({ ...configuration!, constraints: { ...configuration!.constraints, [key]: value } });
  }
}

function EliteWorkflowGuide({
  status,
  queue,
  finalCandidateCount
}: {
  status: ResearchChampionStatus | null;
  queue: ChampionValidationQueue | null;
  finalCandidateCount: number;
}) {
  const backlog = status?.eligible_promoted_jobs ?? 0;
  const champions = status?.research_champions ?? 0;
  const finalElites = status?.final_elites ?? finalCandidateCount;
  const waiting = queue ? queue.pending_validation + queue.needs_more_data : champions;
  const graduated = queue?.graduated_elites ?? status?.graduated_elites ?? 0;
  return (
    <section className="eliteWorkflowGuide" aria-label="Elite graduation workflow">
      <div className={champions > 0 ? "done" : "active"}>
        <span>01</span>
        <strong>Import research champions</strong>
        <p>{backlog > 0 ? `${backlog.toLocaleString()} promoted jobs are waiting. Import deduplicates and imports all of them.` : "No promoted-job backlog is waiting."}</p>
      </div>
      <div className={waiting > 0 ? "active" : champions > 0 ? "done" : ""}>
        <span>02</span>
        <strong>Validate champions</strong>
        <p>{waiting > 0 ? `${waiting.toLocaleString()} champion${waiting === 1 ? "" : "s"} still owe out-of-sample, cross-asset and stress evidence.` : champions > 0 ? "Every imported champion has a verdict." : "Imported champions queue for validation here."}</p>
      </div>
      <div className={finalElites > 0 ? "active" : ""}>
        <span>03</span>
        <strong>Build final portfolio</strong>
        <p>{finalElites.toLocaleString()} final elite row{finalElites === 1 ? "" : "s"} reach the solver{graduated ? `, ${graduated.toLocaleString()} of them graduated through validation` : ""}.</p>
      </div>
    </section>
  );
}

const VALIDATION_STATE_COPY: Record<ChampionValidationState, { label: string; tone: string }> = {
  pending_validation: { label: "Pending", tone: "pending" },
  validating: { label: "Running", tone: "running" },
  validated: { label: "Validated", tone: "validated" },
  failed_validation: { label: "Failed", tone: "failed" },
  needs_more_data: { label: "Needs data", tone: "blocked" }
};

function EliteValidationQueue({
  status,
  queue,
  result,
  diagnostics,
  error,
  busy,
  progress,
  onValidate,
  onStop
}: {
  status: ResearchChampionStatus | null;
  queue: ChampionValidationQueue | null;
  result: ChampionValidationRunResult | null;
  diagnostics: ChampionValidationDiagnostics | null;
  error: string | null;
  busy: string | null;
  progress: { examined: number; validated: number; remaining: number } | null;
  onValidate: (revalidate: boolean) => void;
  onStop: () => void;
}) {
  const champions = queue?.research_champions ?? status?.research_champions ?? 0;
  if (!champions && !result) return null;
  const pending = queue?.pending_validation ?? status?.pending_validation ?? 0;
  const needsData = queue?.needs_more_data ?? status?.needs_more_data ?? 0;
  const failed = queue?.failed_validation ?? status?.failed_validation ?? 0;
  const graduated = queue?.graduated_elites ?? status?.graduated_elites ?? 0;
  const rows = queue?.queue ?? [];
  const running = busy === "validate" || busy === "revalidate";
  const retryable = needsData + failed;

  return (
    <section className="eliteValidationQueue">
      <div className="eliteValidationLead">
        <span className="eyebrow">Step 2</span>
        <h2>Validate the imported champions</h2>
        <p>
          A champion is a winner inside the exact backtest that found it. Validation re-runs it where the search never
          looked: a held-out later period, other symbols, the sibling timeframe, doubled costs, and a duplication check
          against the elites that already exist. Passing every gate is what makes a champion a final elite.
        </p>
        {queue?.gates?.length ? (
          <ul className="eliteGateLegend">
            {queue.gates.map((gate) => <li key={gate.gate_id}>{gate.label}</li>)}
          </ul>
        ) : null}
      </div>

      <div className="eliteValidationSummary">
        <div className="eliteChampionMetrics">
          <Metric label="Pending" value={pending} />
          <Metric label="Needs data" value={needsData} />
          <Metric label="Failed" value={failed} />
          <Metric label="Graduated" value={graduated} tone={graduated ? "safe" : undefined} />
        </div>
        <div className="eliteValidationAction">
          <strong>Run validation for the whole queue</strong>
          <span>
            The queue is drained in timed batches, so progress appears as it goes rather than after everything
            finishes. Each champion executes the full battery of backtests, so this is slow on purpose — nothing is
            graduated on the strength of the original result alone.
          </span>
          <div className="eliteValidationButtons">
            <button className="button" disabled={running || !pending} onClick={() => onValidate(false)}>
              {busy === "validate" ? <LoaderCircle className="spin" size={16} /> : <ShieldCheck size={16} />}
              {busy === "validate" ? "Validating…" : pending ? `Run validation for all ${pending.toLocaleString()} pending champions` : "No champion is pending"}
            </button>
            {retryable ? (
              <button className="eliteTextButton" disabled={running} onClick={() => onValidate(true)}>
                <RefreshCw className={busy === "revalidate" ? "spin" : ""} size={14} />
                {busy === "revalidate" ? "Re-checking…" : `Re-check all ${retryable.toLocaleString()} blocked or failed champion${retryable === 1 ? "" : "s"}`}
              </button>
            ) : null}
          </div>
          {progress ? (
            <div className="eliteValidationProgress">
              <span>
                {progress.examined.toLocaleString()} validated so far · {progress.validated.toLocaleString()} graduated ·{" "}
                {progress.remaining.toLocaleString()} still pending
              </span>
              <button className="eliteTextButton" onClick={onStop}>
                Stop after this batch
              </button>
            </div>
          ) : null}
        </div>
      </div>

      {error ? (
        <div className="eliteValidationNotice error">
          <AlertTriangle size={16} />
          <span><strong>Validation stopped</strong>{error}</span>
        </div>
      ) : null}

      {result ? <ValidationRunSummary result={result} /> : null}
      {rows.length ? <ValidationQueueTable rows={rows} /> : null}
      {diagnostics?.by_gate?.length ? <ValidationGateDiagnostics diagnostics={diagnostics} /> : null}
    </section>
  );
}

function ValidationRunSummary({ result }: { result: ChampionValidationRunResult }) {
  return (
    <div className="eliteValidationRun">
      <h3>
        Last run: {result.examined} champion{result.examined === 1 ? "" : "s"} examined · {result.validated} validated ·{" "}
        {result.failed_validation} failed · {result.needs_more_data} need more data
        {result.errors ? ` · ${result.errors} could not run` : ""}
      </h3>
      <p className="eliteValidationPolicy">
        <LockKeyhole size={14} /> Thresholds weakened: {String(result.thresholds_weakened)}. A gate that could not be
        measured counts as missing evidence, never as a pass.
      </p>
      <div className="eliteValidationOutcomes">
        {result.outcomes.map((outcome) => (
          <article key={outcome.elite_candidate_id} className={`outcome ${outcome.status}`}>
            <header>
              <strong>{outcome.symbol} · {outcome.timeframe}</strong>
              <em>{outcome.status === "error" ? "Could not run" : VALIDATION_STATE_COPY[outcome.status]?.label ?? outcome.status}</em>
            </header>
            <p>{outcome.reason}</p>
            <small>
              {outcome.gates_passed ?? 0} passed · {outcome.gates_failed ?? 0} failed · {outcome.gates_inconclusive ?? 0} unmeasured
              {outcome.backtests_executed ? ` · ${outcome.backtests_executed} backtests` : ""}
              {outcome.runtime_ms ? ` · ${(outcome.runtime_ms / 1000).toFixed(1)}s` : ""}
            </small>
            {outcome.failed_gates?.length ? <small className="failedGates">Failed: {outcome.failed_gates.map(title).join(", ")}</small> : null}
            {outcome.inconclusive_gates?.length ? <small>Unmeasured: {outcome.inconclusive_gates.map(title).join(", ")}</small> : null}
          </article>
        ))}
      </div>
    </div>
  );
}

function ValidationQueueTable({ rows }: { rows: ChampionValidationQueue["queue"] }) {
  return (
    <div className="eliteValidationTable">
      <h3>Validation queue ({rows.length} shown)</h3>
      <table>
        <thead>
          <tr><th>Champion</th><th>Family</th><th>PF</th><th>Trades</th><th>Drawdown</th><th>State</th><th>Reason</th></tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.elite_candidate_id}>
              <td><strong>{row.symbol}</strong> · {row.timeframe}</td>
              <td>{row.family_id}</td>
              <td>{row.profit_factor.toFixed(2)}</td>
              <td>{row.trade_count}</td>
              <td>{(row.max_drawdown * 100).toFixed(1)}%</td>
              <td><span className={`eliteStateChip ${VALIDATION_STATE_COPY[row.validation_state]?.tone ?? ""}`}>{VALIDATION_STATE_COPY[row.validation_state]?.label ?? row.validation_state}</span></td>
              <td className="reason">{row.validation_state_reason ?? "Not validated yet."}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ValidationGateDiagnostics({ diagnostics }: { diagnostics: ChampionValidationDiagnostics }) {
  const byGate = new Map<string, { label: string; passed: number; failed: number; inconclusive: number }>();
  for (const row of diagnostics.by_gate) {
    const entry = byGate.get(row.gate_id) ?? { label: row.label, passed: 0, failed: 0, inconclusive: 0 };
    if (row.status === "passed") entry.passed += row.candidates;
    if (row.status === "failed") entry.failed += row.candidates;
    if (row.status === "inconclusive") entry.inconclusive += row.candidates;
    byGate.set(row.gate_id, entry);
  }
  const gates = [...byGate.entries()].sort((left, right) => right[1].failed - left[1].failed);
  return (
    <div className="eliteValidationDiagnostics">
      <h3>Where champions are dying</h3>
      <p>Grouped by gate across the latest verdict for every champion. This is what decides which research families are worth expanding next.</p>
      <div className="eliteGateGrid">
        {gates.map(([gateId, entry]) => (
          <div key={gateId}>
            <strong>{entry.label}</strong>
            <span><b className="passed">{entry.passed}</b> passed · <b className="failed">{entry.failed}</b> failed · <b>{entry.inconclusive}</b> unmeasured</span>
          </div>
        ))}
      </div>
      {diagnostics.by_group?.length ? (
        <table>
          <thead><tr><th>Family</th><th>Symbol</th><th>Timeframe</th><th>Validated</th><th>Failed</th><th>Needs data</th></tr></thead>
          <tbody>
            {diagnostics.by_group.slice(0, 12).map((row) => (
              <tr key={`${row.family_id}-${row.symbol}-${row.timeframe}`}>
                <td>{row.family_id}</td>
                <td>{row.symbol}</td>
                <td>{row.timeframe}</td>
                <td>{row.validated}</td>
                <td>{row.failed_validation}</td>
                <td>{row.needs_more_data}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

function PortfolioReview({ result, analytics, members }: { result: ElitePortfolioResult; analytics: Record<string, any>; members: Array<Record<string, any>> }) {
  const infeasible = result.status === "infeasible";
  const verification = result.verification;
  const verifiedInfeasible = Boolean(result.verified_infeasible);
  const heuristicMiss = Boolean(result.heuristic_miss);
  const unverified = infeasible && !verifiedInfeasible;
  const distributions = [
    ["Direction", analytics.direction_distribution],
    ["Timeframe", analytics.timeframe_distribution],
    ["Family", analytics.family_distribution]
  ] as const;
  const similarityConflicts = (result.conflicts ?? []).filter((row) => row.conflict_type === "PARAMETER_SIMILARITY");
  return (
    <section className={`elitePanel eliteReview ${infeasible ? "infeasible" : ""}`}>
      <header>
        <div>
          <span className="eyebrow">Construction result</span>
          <h2>{infeasible ? (verifiedInfeasible ? "No portfolio satisfies every constraint — exactly verified" : "The fast constructor found nothing — verification incomplete") : `${result.maximum_feasible_size ?? members.length} members ready for review`}</h2>
        </div>
        {infeasible ? <AlertTriangle size={21} /> : <BarChart3 size={21} />}
      </header>
      {heuristicMiss ? (
        <p className="eliteReviewLead heuristicMiss">
          <ShieldCheck size={15} /> The bounded greedy constructor missed this portfolio. The exact exhaustive verifier found it and it is shown below — nothing was relaxed.
        </p>
      ) : null}
      {verifiedInfeasible ? (
        <p className="eliteReviewLead">Exact exhaustive verification searched every candidate combination in this pool ({verification?.pool_size ?? "—"} candidates, {verification?.nodes_explored ?? "—"} nodes) and confirmed no feasible portfolio exists. Zero constraints were relaxed. Review the ranked binding constraints and deliberately change evidence requirements only if your research policy changes.</p>
      ) : null}
      {unverified ? (
        <p className="eliteReviewLead warning">
          The bounded greedy constructor found no portfolio, but exact verification {verification?.ran ? "did not complete within its search budget" : `did not run (pool of ${verification?.pool_size ?? "?"} candidates exceeds the ${verification?.verification_limit ?? "?"}-candidate verification limit)`}. This is <strong>not</strong> a proven infeasibility — treat it as unresolved rather than "no portfolio exists."
        </p>
      ) : null}
      {!infeasible ? <p className="eliteReviewLead">The largest feasible portfolio found by the bounded deterministic constructor{heuristicMiss ? " (recovered by exact verification)" : ""}. Approval remains tied to this exact evidence snapshot.</p> : null}
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
      {similarityConflicts.length ? (
        <div className="eliteSimilarity">
          <h3>Parameter-similarity conflicts ({similarityConflicts.length})</h3>
          {similarityConflicts.slice(0, 12).map((row, index) => (
            <details key={`${row.left_candidate_id}-${row.right_candidate_id}-${index}`}>
              <summary>
                <span>{row.left_candidate_id} ↔ {row.right_candidate_id}</span>
                <strong>{number(row.evidence?.coefficient)}</strong>
              </summary>
              <p>{row.evidence?.reason}</p>
              {Array.isArray(row.evidence?.compared_parameters) ? (
                <table>
                  <thead><tr><th>Parameter</th><th>Left</th><th>Right</th><th>Normalized diff</th><th>Similarity</th></tr></thead>
                  <tbody>
                    {row.evidence.compared_parameters.map((param: Record<string, any>) => (
                      <tr key={param.parameter}>
                        <td>{param.parameter}</td>
                        <td>{param.missing_on_one_side ? "—" : String(param.left_value)}</td>
                        <td>{param.missing_on_one_side ? "—" : String(param.right_value)}</td>
                        <td>{param.normalized_difference == null ? "n/a" : number(param.normalized_difference)}</td>
                        <td>{number(param.key_similarity)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
            </details>
          ))}
        </div>
      ) : null}
      {members.length ? <div className="eliteMembers"><h3>Selected members</h3>{members.map((row, index) => <article key={`${row.candidate_id}-${row.symbol ?? "variant"}-${row.timeframe ?? "all"}`}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{row.symbol ? `${row.symbol} · ${row.timeframe}` : row.candidate_id}</strong><small>{row.strategy_family ?? row.strategy_direction ?? "Selected strategy variant"}</small></div><em>{title(row.execution_capability ?? "selected")}</em></article>)}</div> : null}
    </section>
  );
}

function ResearchChampionIntake({
  status,
  result,
  dedupeResult,
  busy,
  dedupeBusy,
  onImport,
  onDedupe
}: {
  status: ResearchChampionStatus | null;
  result: ResearchChampionImportResult | null;
  dedupeResult: ChampionDedupeResult | null;
  busy: boolean;
  dedupeBusy: boolean;
  onImport: () => void;
  onDedupe: () => void;
}) {
  const hasChampions = Boolean(status?.research_champions);
  return (
    <section className="eliteChampionIntake">
      <div>
        <span className="eyebrow">Step 1</span>
        <h2>{hasChampions ? "Champion import is working" : "Do this now: import research champions"}</h2>
        <p>
          {hasChampions
            ? "You already have imported research champions. Import again only to pull in newly promoted jobs."
            : "This takes every eligible promoted research job, removes near-duplicates, and imports the rest as champions. They are review candidates, not live-trading elites."}
        </p>
      </div>
      <div className="eliteChampionMetrics">
        <Metric label="Eligible backlog" value={status?.eligible_promoted_jobs ?? "—"} />
        <Metric label="Imported champions" value={status?.research_champions ?? "—"} />
        <Metric label="Final elites" value={status?.final_elites ?? "—"} />
        <Metric label="Symbols" value={status?.symbols ?? "—"} />
      </div>
      {/* A backlog of 0 alongside eligible job rows is the normal steady state
          once a strategy is already represented, not a stuck queue. Saying so
          here stops it reading as a broken import. */}
      {status && !status.eligible_promoted_jobs && (status.duplicate_of_existing_champion ?? 0) > 0 ? (
        <div className="eliteChampionResult">
          <strong>Nothing new to import</strong>
          <span className="eliteImportExplanation">
            {status.eligible_jobs_scanned?.toLocaleString()} eligible job
            {status.eligible_jobs_scanned === 1 ? "" : "s"} passed the thresholds, but{" "}
            {status.duplicate_of_existing_champion?.toLocaleString()} are the same strategy as a champion you
            already have — same symbol, timeframe, family, blocks, execution parameters and direction. Importing
            them again would duplicate work, not add coverage.
          </span>
        </div>
      ) : null}
      {result ? (
        <div className="eliteChampionResult">
          <strong>Import completed: {result.imported} champions added</strong>
          <span className="eliteImportExplanation">
            {result.imported === 0 && result.examined > 0
              ? `All ${result.examined.toLocaleString()} eligible job${result.examined === 1 ? "" : "s"} already match a champion you have. Nothing was skipped by mistake.`
              : `${result.dedupe_clusters_seen} new dedupe cluster${result.dedupe_clusters_seen === 1 ? "" : "s"} found · ${result.already_covered_clusters} already covered by an existing champion. Final elites created: ${result.final_elites_created}. Thresholds weakened: ${String(result.thresholds_weakened)}. Next step: validation.`}
          </span>
        </div>
      ) : null}
      {dedupeResult ? (
        <div className="eliteChampionResult">
          <strong>
            {dedupeResult.champions_demoted
              ? `Cleanup removed ${dedupeResult.champions_demoted} duplicate champion${dedupeResult.champions_demoted === 1 ? "" : "s"}`
              : "No duplicate champions found"}
          </strong>
          <span>{dedupeResult.duplicate_clusters} duplicate cluster{dedupeResult.duplicate_clusters === 1 ? "" : "s"} of {dedupeResult.clusters_examined} examined · duplicates demoted, never deleted</span>
        </div>
      ) : null}
      <div className="eliteChampionActions">
        <button className="button" disabled={busy || !status?.eligible_promoted_jobs} onClick={onImport}>
          <Sparkles size={16} />
          {busy
            ? "Importing champions..."
            : status?.eligible_promoted_jobs
              ? `Import all ${status.eligible_promoted_jobs.toLocaleString()} eligible champions`
              : "No eligible backlog to import"}
        </button>
        {hasChampions ? (
          <button className="eliteTextButton" disabled={dedupeBusy} onClick={onDedupe} title="A different campaign run can produce the same effective strategy under a new candidate_id. This finds those and keeps one per strategy.">
            <RefreshCw className={dedupeBusy ? "spin" : ""} size={14} />
            {dedupeBusy ? "Checking for duplicates…" : "Remove duplicate champions"}
          </button>
        ) : null}
      </div>
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

const FALLBACK_HARD_RULES: ElitePortfolioHardRule[] = [
  { id: "SYMBOL_FAMILY_DUPLICATE", label: "One member per symbol-family pair", description: "At most one candidate may occupy a given (symbol, family) pair, even when the per-symbol and per-family caps would otherwise allow two." },
  { id: "PARAMETER_SIMILARITY", label: "Maximum parameter similarity 0.90", description: "Two candidates whose strategy parameters are more than 90% similar can never appear in the same portfolio." },
  { id: "SIGNAL_CORRELATION_LIMIT", label: "Signal-correlation conflict rule", description: "Candidates whose signal-exposure series correlate above the configured signal-correlation limit are a hard conflict." },
  { id: "STRATEGY_RETURN_CORRELATION_LIMIT", label: "Strategy-return-correlation rule", description: "Candidates whose strategy-return series correlate above the configured strategy-return-correlation limit are a hard conflict." },
  { id: "TIMEFRAME_50_PERCENT_CAP", label: "Exact timeframe balance (2 × count ≤ total)", description: "No single timeframe may exceed half the portfolio. With exactly two timeframes selected this forces an exact 50/50 split, only reachable at even portfolio sizes." }
];

function hardRules(options: ElitePortfolioOptions): ElitePortfolioHardRule[] {
  return options.hard_rules?.length ? options.hard_rules : FALLBACK_HARD_RULES;
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

function PortfolioProfilePicker({
  options,
  configuration,
  recommendation,
  busy,
  onSelect,
  onRecommend,
  onApplyRecommendation
}: {
  options: ElitePortfolioOptions;
  configuration: ElitePortfolioConfiguration;
  recommendation: ElitePortfolioRecommendation | null;
  busy: string | null;
  onSelect: (profile: string) => void;
  onRecommend: () => void;
  onApplyRecommendation: (profile: string) => void;
}) {
  const profiles = options.profiles ?? [];
  if (!profiles.length) return null;
  const active = configuration.profile ?? options.default_profile ?? "strict_diversified";
  return (
    <section className="elitePanel">
      <header>
        <div>
          <span className="eyebrow">00 · Portfolio shape</span>
          <h2>Pick how spread the portfolio must be</h2>
        </div>
        <Layers3 size={20} />
      </header>
      <p className="eliteProfileLead">
        Profiles change portfolio size and how far members must be spread. They never change a quality threshold, a
        correlation limit, or the parameter-similarity rule — those are identical in every profile, and the API rejects
        a configuration that tries to weaken them.
      </p>
      <div className="eliteProfileGrid">
        {profiles.map((profile) => (
          <button
            key={profile.id}
            type="button"
            className={`eliteProfileCard ${active === profile.id ? "active" : ""} ${profile.diversified ? "" : "undiversified"}`}
            onClick={() => onSelect(profile.id)}
          >
            <header>
              <strong>{profile.label}</strong>
              {active === profile.id ? <Check size={14} /> : null}
            </header>
            <p>{profile.summary}</p>
            <small>{profile.intended_use}</small>
            <dl>
              <div><dt>Size</dt><dd>{profile.resolved_constraints.minimum_portfolio_size}–{profile.resolved_constraints.maximum_portfolio_size}</dd></div>
              <div><dt>Assets</dt><dd>≥ {profile.resolved_constraints.minimum_unique_assets}</dd></div>
              <div><dt>Families</dt><dd>≥ {profile.resolved_constraints.minimum_families}</dd></div>
              <div><dt>Timeframe cap</dt><dd>{profile.resolved_constraints.timeframe_cap_numerator}/{profile.resolved_constraints.timeframe_cap_denominator}</dd></div>
            </dl>
            {profile.warning ? <span className="eliteProfileWarning"><AlertTriangle size={12} />{profile.warning}</span> : null}
            <span className="eliteProfileApply">{active === profile.id ? "Applied" : `Apply ${profile.label}`}</span>
          </button>
        ))}
      </div>

      <div className="eliteRecommendation">
        {recommendation ? (
          <>
            <strong>{recommendation.recommended_label ?? "No feasible profile"}</strong>
            <p>{recommendation.reason}</p>
            {recommendation.constraints_relaxed_versus_strict.length ? (
              <ul>
                {recommendation.constraints_relaxed_versus_strict.map((row) => (
                  <li key={row.setting}>{row.label}: {String(row.strict_value)} → {String(row.profile_value)}</li>
                ))}
              </ul>
            ) : null}
            <small className="eliteProfilePolicy">
              <LockKeyhole size={12} /> Correlation, parameter-similarity, quality and validation requirements unchanged:{" "}
              {String(recommendation.protected_constraints_unchanged)}
            </small>
            <div className="eliteProfileFeasibility">
              {recommendation.profiles.map((row) => (
                <span key={row.profile} className={row.feasible ? "feasible" : "infeasible"}>
                  {row.label}: {row.feasible ? `${row.size} member${row.size === 1 ? "" : "s"}` : "no portfolio"}
                </span>
              ))}
            </div>
            {recommendation.recommended_profile && recommendation.recommended_profile !== active ? (
              <button className="button" onClick={() => onApplyRecommendation(recommendation.recommended_profile!)} disabled={Boolean(busy)}>
                <Sparkles size={15} />Use recommended {recommendation.recommended_label} profile
              </button>
            ) : null}
          </>
        ) : (
          <>
            <strong>Not sure which profile fits?</strong>
            <p>Test every profile against the current elite pool and recommend the strictest one that actually produces a portfolio.</p>
            <button className="button secondary" onClick={onRecommend} disabled={Boolean(busy)}>
              {busy === "recommend" ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}
              {busy === "recommend" ? "Testing profiles…" : "Recommend a feasible profile"}
            </button>
          </>
        )}
      </div>
    </section>
  );
}

function PortfolioFeasibilityPanel({ result }: { result: ElitePortfolioResult }) {
  const analysis = result.blocking_analysis;
  if (!analysis) return null;
  const rejections = result.rejection_explanations ?? [];
  const counts = new Map<string, number>();
  for (const row of rejections) {
    for (const label of (row.reason_labels ?? row.reasons ?? []) as string[]) {
      counts.set(label, (counts.get(label) ?? 0) + 1);
    }
  }
  const ranked = [...counts.entries()].sort((left, right) => right[1] - left[1]);
  return (
    <section className="elitePanel eliteFeasibility">
      <header>
        <div>
          <span className="eyebrow">Eligibility</span>
          <h2>{analysis.feasible ? "What the solver had to work with" : "Why there is no portfolio yet"}</h2>
        </div>
        {analysis.feasible ? <ShieldCheck size={20} /> : <AlertTriangle size={20} />}
      </header>

      <div className="eliteAnalyticsStrip">
        <Metric label="Elite variants" value={(result.eligible_count ?? 0) + (result.excluded_count ?? 0)} />
        <Metric label="Eligible" value={result.eligible_count ?? 0} />
        <Metric label="Excluded" value={result.excluded_count ?? 0} />
        <Metric label="Max feasible size" value={result.maximum_feasible_size ?? 0} />
      </div>

      {analysis.primary_blocker ? (
        <div className="elitePrimaryBlocker">
          <strong>Biggest blocker: {analysis.primary_blocker.label}</strong>
          <p>{analysis.primary_blocker.detail}</p>
        </div>
      ) : null}

      <div className="elitePoolCoverage">
        <div><span>Symbols in pool</span><strong>{analysis.pool_symbols.join(", ") || "—"}</strong></div>
        <div><span>Families in pool</span><strong>{analysis.pool_families.join(", ") || "—"}</strong></div>
        <div><span>Timeframes in pool</span><strong>{analysis.pool_timeframes.join(", ") || "—"}</strong></div>
      </div>

      {ranked.length ? (
        <div className="eliteExclusionReasons">
          <h3>Why elites were excluded from the pool</h3>
          {ranked.map(([label, count]) => (
            <div key={label}><span>{label}</span><strong>{count}</strong></div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function PaperLabPanel({
  result,
  phase,
  busy,
  onPreview,
  onSave,
  onApprove
}: {
  result: ElitePortfolioResult | null;
  phase: "idle" | "preview" | "saved";
  busy: string | null;
  onPreview: () => void;
  onSave: () => void;
  onApprove: () => void;
}) {
  const rejections = result?.rejection_explanations ?? [];
  const counts = new Map<string, number>();
  for (const row of rejections) {
    for (const label of (row.reason_labels ?? row.reasons ?? []) as string[]) {
      counts.set(label, (counts.get(label) ?? 0) + 1);
    }
  }
  const ranked = [...counts.entries()].sort((left, right) => right[1] - left[1]);
  const snapshotHash = result?.snapshot?.decision_hash ?? result?.snapshot?.snapshot_hash ?? null;
  const running = busy === "paperlab-preview" || busy === "paperlab-save" || busy === "paperlab-approve";
  return (
    <section className="elitePaperLab">
      <header>
        <div>
          <span className="eyebrow">Execution-testing mode</span>
          <h2>All Validated Elites Paper Lab</h2>
          <p>
            Includes every validated elite eligible for Alpaca Paper — long direction, external-capable, unique
            candidate/symbol/timeframe, authoritative lineage. Nothing is excluded for correlation, shared symbols, or
            shared families. This is not a diversified portfolio: it exists to exercise activation, approval, preflight
            and execution across the whole validated pool, and strategies here may be duplicated bets on the same edge.
          </p>
        </div>
        <AlertTriangle size={22} />
      </header>

      {result ? (
        <>
          <div className="eliteAnalyticsStrip">
            <Metric label="Eligible" value={result.eligible_count ?? 0} tone="safe" />
            <Metric label="Excluded" value={result.excluded_count ?? 0} />
            <Metric label="Included in run" value={result.maximum_feasible_size ?? 0} />
            <Metric label="Status" value={result.status === "review_ready" ? "Ready" : "No elites qualify"} />
          </div>
          {ranked.length ? (
            <div className="eliteExclusionReasons">
              <h3>Why elites were excluded</h3>
              {ranked.map(([label, count]) => (
                <div key={label}><span>{label}</span><strong>{count}</strong></div>
              ))}
            </div>
          ) : null}
        </>
      ) : null}

      <div className="elitePaperLabActions">
        <button className="button secondary" disabled={running} onClick={onPreview}>
          {busy === "paperlab-preview" ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}
          {busy === "paperlab-preview" ? "Reading elite pool…" : "Preview all validated elites"}
        </button>
        {phase === "preview" && result?.status === "review_ready" ? (
          <button className="button" disabled={running} onClick={onSave}>
            {busy === "paperlab-save" ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={16} />}
            {busy === "paperlab-save" ? "Saving…" : "Save immutable paper lab run"}
          </button>
        ) : null}
        {phase === "saved" && result?.id && snapshotHash ? (
          <button className="button" disabled={running} onClick={onApprove}>
            {busy === "paperlab-approve" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
            {busy === "paperlab-approve" ? "Approving…" : "Approve paper lab snapshot"}
          </button>
        ) : null}
        {result?.status === "approved" ? <span className="eliteBulkResult">Approved — see Step 4 below to activate.</span> : null}
      </div>
    </section>
  );
}
