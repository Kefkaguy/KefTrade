"use client";

import { motion, useReducedMotion } from "framer-motion";
import {
  ArrowRight,
  Bitcoin,
  Brain,
  Check,
  ChartNoAxesCombined,
  CircuitBoard,
  Crosshair,
  Database,
  Globe2,
  Layers3,
  LoaderCircle,
  Search,
  ShieldCheck,
  Sparkles,
  TrendingUp
} from "lucide-react";
import {
  buildResearchSelection,
  CRYPTO_RESEARCH_ASSETS,
  FALLBACK_RESEARCH_ASSETS,
  MAX_TARGETED_CANDIDATES,
  MAX_PROFILE_ASSETS,
  RESEARCH_SCOPES,
  STRATEGY_FAMILIES,
  VALIDATION_METHODS,
  type ResearchAsset,
  type ResearchAssetId,
  type ResearchScopeId,
  type ResearchSelection
} from "@/lib/home-research";
import {
  fetchResearchCommandCenter,
  getResearchLearning,
  getSymbols,
  type ResearchCommandCenter,
  type ResearchLearningSummary
} from "@/lib/api";
import { useEffect, useMemo, useState } from "react";

type ResearchBuilderProps = {
  launching: boolean;
  onLaunch: (selection: ResearchSelection) => void;
};

type EvidenceTier = "established" | "exploration" | "deprioritized";

type AssetEvidence = {
  symbol: string;
  tier: EvidenceTier;
  score: number;
  confidence: "High" | "Medium" | "Low";
  campaigns: number;
  validationRuns: number;
  researchCandidates: number;
  eliteCandidates: number;
  forwardDeployments: number;
  profitFactor: number;
  expectancy: number;
  tradeCount: number;
  drawdown: number;
  bestFamily?: string | null;
  bestTimeframe?: string | null;
  dominantFailure?: string | null;
  repairSuccess?: number | null;
  reasons: string[];
};

const scopeIcons = {
  single: Crosshair,
  core: Layers3,
  technology: CircuitBoard,
  crypto: Bitcoin,
  index: ChartNoAxesCombined,
  universe: Globe2
} as const;

const preferredSymbols = ["TSLA", "NVDA", "AAPL", "MSFT", "SPY", "QQQ", "AMZN", "META", "GOOGL", "AMD"];
const fallbackStocks = FALLBACK_RESEARCH_ASSETS.filter((asset) => asset.market !== "Crypto");
const MAX_RANDOM_STOCKS = 100;
const DEFAULT_EVIDENCE_ALLOCATION = 90;

export function ResearchBuilder({ launching, onLaunch }: ResearchBuilderProps) {
  const reduceMotion = useReducedMotion();
  const [scopeId, setScopeId] = useState<ResearchScopeId>("core");
  const [universeMode, setUniverseMode] = useState<"random" | "established">("random");
  const [evidenceAllocation, setEvidenceAllocation] = useState(DEFAULT_EVIDENCE_ALLOCATION);
  const [stockCatalog, setStockCatalog] = useState<ResearchAsset[]>(fallbackStocks);
  const [assetIds, setAssetIds] = useState<ResearchAssetId[]>(["TSLA", "NVDA", "AAPL", "MSFT", "AMD", "META", "GOOGL", "AMZN", "SPY", "QQQ"]);
  const [learningSummary, setLearningSummary] = useState<ResearchLearningSummary | null>(null);
  const [commandCenter, setCommandCenter] = useState<ResearchCommandCenter | null>(null);
  const [profileAssetId, setProfileAssetId] = useState<ResearchAssetId | null>(null);
  const [learningState, setLearningState] = useState<"loading" | "ready" | "unavailable">("loading");
  const [catalogState, setCatalogState] = useState<"loading" | "ready" | "fallback">("loading");
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const allAssets = useMemo(() => [...stockCatalog, ...CRYPTO_RESEARCH_ASSETS], [stockCatalog]);
  const assetById = useMemo(() => new Map(allAssets.map((asset) => [asset.id, asset])), [allAssets]);
  const evidenceGuidance = useMemo(() => establishedGuidance(learningSummary, commandCenter), [commandCenter, learningSummary]);
  const evidenceBySymbol = useMemo(() => new Map(evidenceGuidance.assetEvidence.map((row) => [row.symbol.toUpperCase(), row])), [evidenceGuidance.assetEvidence]);
  const establishedAssets = useMemo(
    () => learnedAssets(stockCatalog, evidenceGuidance.assets, evidenceGuidance.deprioritizedAssets, evidenceAllocation),
    [evidenceAllocation, evidenceGuidance.assets, evidenceGuidance.deprioritizedAssets, stockCatalog]
  );
  const selectedAssets = useMemo(
    () => assetIds.map((id) => assetById.get(id)).filter((asset): asset is ResearchAsset => Boolean(asset)),
    [assetById, assetIds]
  );
  const profileAsset = profileAssetId ? assetById.get(profileAssetId) ?? null : null;
  const profileEvidence = profileAsset ? evidenceBySymbol.get(profileAsset.apiSymbol.toUpperCase()) ?? null : null;
  const selection = useMemo(
    () => buildResearchSelection(scopeId, selectedAssets, {
      universeMode,
      evidenceAllocationPct: universeMode === "established" ? evidenceAllocation : undefined,
      guidanceSnapshotKey: universeMode === "established" ? evidenceGuidance.snapshotKey : undefined,
      establishedStrategyFamilies: universeMode === "established" ? evidenceGuidance.strategyFamilies : undefined,
      timeframes: universeMode === "established" && evidenceGuidance.timeframes.length ? evidenceGuidance.timeframes : undefined
    }),
    [evidenceAllocation, evidenceGuidance, scopeId, selectedAssets, universeMode]
  );
  const selectedStockCount = selectedAssets.filter((asset) => asset.market !== "Crypto").length;
  const readyStockCatalog = useMemo(() => stockCatalog.filter(isResearchReadyStock), [stockCatalog]);
  const maxStockSelection = Math.max(1, Math.min(stockCatalog.length, MAX_PROFILE_ASSETS, MAX_RANDOM_STOCKS));
  const maxReadyStockSelection = Math.max(1, Math.min(readyStockCatalog.length || stockCatalog.length, MAX_PROFILE_ASSETS, MAX_RANDOM_STOCKS));
  const maxEstablishedSelection = Math.max(1, Math.min(establishedAssets.length || readyStockCatalog.length || stockCatalog.length, MAX_PROFILE_ASSETS, MAX_RANDOM_STOCKS));
  const visibleAssets = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    const baseAssets = universeMode === "established" ? selectedFirst(learnedAssets(stockCatalog, evidenceGuidance.assets, evidenceGuidance.deprioritizedAssets, evidenceAllocation), assetIds) : allAssets;
    const matches = query
      ? allAssets.filter((asset) => asset.id.toLowerCase().includes(query) || asset.name.toLowerCase().includes(query))
      : universeMode === "established" ? baseAssets : prioritizeAssets(allAssets);
    return selectedFirst(matches, assetIds).slice(0, 40);
  }, [allAssets, assetIds, evidenceAllocation, evidenceGuidance.assets, evidenceGuidance.deprioritizedAssets, searchQuery, stockCatalog, universeMode]);

  useEffect(() => {
    let active = true;
    void Promise.allSettled([getSymbols(), getResearchLearning(), fetchResearchCommandCenter()])
      .then(([symbolsResult, learningResult, commandCenterResult]) => {
        if (!active) return;
        if (learningResult.status === "fulfilled") {
          setLearningSummary(learningResult.value);
        }
        if (commandCenterResult.status === "fulfilled") {
          setCommandCenter(commandCenterResult.value);
        }
        if (learningResult.status === "fulfilled" || commandCenterResult.status === "fulfilled") {
          const hasLearning = learningResult.status === "fulfilled" && Boolean(learningResult.value.global_learning?.snapshot_key);
          const hasProposal = commandCenterResult.status === "fulfilled" && Boolean(commandCenterResult.value.next_campaign_proposal);
          setLearningState(hasLearning || hasProposal ? "ready" : "unavailable");
        } else {
          setLearningState("unavailable");
        }
        if (symbolsResult.status === "rejected") {
          setCatalogState("fallback");
          setCatalogError(symbolsResult.reason instanceof Error ? symbolsResult.reason.message : "The saved symbol catalog is unavailable.");
          return;
        }
        const symbols = symbolsResult.value;
        const imported = symbols
          .filter((symbol) => symbol.is_active && ["us_equity", "etf"].includes(String(symbol.asset_class).toLowerCase()))
          .map<ResearchAsset>((symbol) => ({
            id: symbol.symbol,
            apiSymbol: symbol.symbol,
            name: symbol.name,
            market: String(symbol.asset_class).toLowerCase() === "etf" ? "ETF" : "Equity",
            exchange: symbol.exchange,
            ready1hCandles: Number(symbol.ready_1h_candles ?? 0),
            ready4hCandles: Number(symbol.ready_4h_candles ?? 0),
            ready1hFeatures: Number(symbol.ready_1h_features ?? 0),
            ready4hFeatures: Number(symbol.ready_4h_features ?? 0),
            researchReady: Boolean(symbol.research_ready),
            latest1hCandleAt: symbol.latest_1h_candle_timestamp ?? null,
            latest4hCandleAt: symbol.latest_4h_candle_timestamp ?? null
          }));
        setStockCatalog(imported.length ? imported : fallbackStocks);
        setCatalogState(imported.length ? "ready" : "fallback");
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (universeMode !== "established") return;
    const count = boundedStockCount(selectedStockCount || maxEstablishedSelection, maxEstablishedSelection);
    const learnedPool = establishedAssets.length ? establishedAssets : prioritizeAssets(readyStockCatalog.length ? readyStockCatalog : stockCatalog);
    setAssetIds(learnedPool.slice(0, count).map((asset) => asset.id));
    setScopeId("custom");
  }, [establishedAssets, maxEstablishedSelection, readyStockCatalog, selectedStockCount, stockCatalog, universeMode]);

  function chooseScope(nextScopeId: Exclude<ResearchScopeId, "custom">) {
    const scope = RESEARCH_SCOPES.find((item) => item.id === nextScopeId);
    if (!scope) return;
    const nextIds = nextScopeId === "universe"
      ? prioritizeAssets(stockCatalog).slice(0, MAX_PROFILE_ASSETS).map((asset) => asset.id)
      : scope.assets.filter((id) => allAssets.some((asset) => asset.id === id));
    if (!nextIds.length) return;
    setScopeId(nextScopeId);
    setAssetIds(nextIds);
  }

  function toggleAsset(assetId: ResearchAssetId) {
    if (universeMode === "established") {
      setProfileAssetId(assetId);
      return;
    }
    if (scopeId === "single") {
      setAssetIds([assetId]);
      return;
    }
    const exists = assetIds.includes(assetId);
    if (exists && assetIds.length === 1) return;
    if (!exists && assetIds.length >= MAX_PROFILE_ASSETS) return;
    setAssetIds(exists ? assetIds.filter((id) => id !== assetId) : [...assetIds, assetId]);
    setScopeId("custom");
  }

  function chooseAssetCount(rawCount: number) {
    if (universeMode === "established") {
      chooseEstablishedAssetCount(rawCount);
      return;
    }
    const pool = readyStockCatalog.length >= rawCount ? readyStockCatalog : stockCatalog;
    const count = boundedStockCount(rawCount, pool.length);
    setAssetIds(prioritizeAssets(pool).slice(0, count).map((asset) => asset.id));
    setScopeId("custom");
  }

  function chooseRandomAssetCount(rawCount: number) {
    setUniverseMode("random");
    const pool = readyStockCatalog.length >= rawCount ? readyStockCatalog : stockCatalog;
    const count = boundedStockCount(rawCount, pool.length);
    setAssetIds(randomStocks(pool, count).map((asset) => asset.id));
    setScopeId("custom");
  }

  function chooseEstablishedAssetCount(rawCount: number) {
    const learnedPool = establishedAssets.length ? establishedAssets : prioritizeAssets(readyStockCatalog.length ? readyStockCatalog : stockCatalog);
    const count = boundedStockCount(rawCount, learnedPool.length);
    setAssetIds(learnedPool.slice(0, count).map((asset) => asset.id));
    setScopeId("custom");
  }

  function chooseUniverseMode(nextMode: "random" | "established") {
    setUniverseMode(nextMode);
    if (nextMode === "established") {
      chooseEstablishedAssetCount(Math.min(Math.max(selectedStockCount, 1), maxEstablishedSelection));
    }
  }

  function chooseRandomDefault() {
    chooseRandomAssetCount(maxReadyStockSelection);
  }

  return (
    <motion.section
      id="research-builder"
      className="researchBuilderSection"
      aria-labelledby="research-builder-title"
      initial={reduceMotion ? false : { opacity: 0, y: 24 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, amount: 0.12 }}
      transition={{ duration: 0.55, ease: [0.22, 1, 0.36, 1] }}
    >
      <header className="builderIntro">
        <span className="eyebrow">Research builder</span>
        <h2 id="research-builder-title">Define the market. KefTrade handles the search.</h2>
        <p>Choose a measured market scope. KefTrade freezes the experiment dataset, profiles behavior, forms one testable hypothesis, and creates at most {MAX_TARGETED_CANDIDATES.toLocaleString()} focused strategy variations.</p>
      </header>

      <div className="researchBuilderShell">
        <div className="builderControls">
          <div className="builderStep">
            <div className="builderStepHeading">
              <span>01</span>
              <div><strong>Research scope</strong><small>Choose the breadth of the search.</small></div>
            </div>
            <div className="scopeSelector" role="list" aria-label="Research scope">
              {RESEARCH_SCOPES.map((scope) => {
                const Icon = scopeIcons[scope.id];
                const selected = scope.id === scopeId;
                const unavailable = scope.id === "universe" && catalogState !== "ready";
                return (
                  <motion.button
                    key={scope.id}
                    type="button"
                    className={`scopeOption ${selected ? "selected" : ""}`}
                    aria-pressed={selected}
                    disabled={unavailable}
                    onClick={() => chooseScope(scope.id)}
                    whileHover={reduceMotion ? undefined : { x: 3 }}
                    whileTap={reduceMotion ? undefined : { scale: 0.99 }}
                  >
                    <Icon size={18} />
                    <span><strong>{scope.label}</strong><small>{scope.description}</small></span>
                    <span className="scopeCheck" aria-hidden="true">{selected ? <Check size={14} /> : <ArrowRight size={14} />}</span>
                  </motion.button>
                );
              })}
            </div>
          </div>

          <div className="builderStep">
            <div className="builderStepHeading">
              <span>02</span>
              <div><strong>Assets</strong><small>Choose the amount, then search or refine individual symbols.</small></div>
            </div>

            <div className="assetCatalogStatus">
              <Database size={16} />
              <div>
                <strong>{catalogState === "loading" ? "Loading saved assets" : `${stockCatalog.length.toLocaleString()} stock assets available`}</strong>
                <small>{catalogState === "ready" ? "Active tradable US equities from the saved symbol catalog" : catalogError ? "Using the local fallback while the saved catalog is unavailable" : "Reading the saved symbol catalog"}</small>
              </div>
              {catalogState === "loading" ? <LoaderCircle className="catalogSpinner" size={16} /> : <Check size={16} />}
            </div>

            {scopeId === "crypto" ? (
              <div className="assetCountControl cryptoSelectionNote">
                <div><strong>Crypto selection</strong><span>{assetIds.length} selected</span></div>
                <p>Choose a stock scope or adjust the asset catalog below to switch back to Alpaca equities.</p>
              </div>
            ) : (
              <div className="assetCountControl">
                <div className="universeModeControl" role="radiogroup" aria-label="Universe mode">
                  <button
                    type="button"
                    className={universeMode === "random" ? "selected" : ""}
                    aria-pressed={universeMode === "random"}
                    onClick={() => chooseUniverseMode("random")}
                  >
                    <Sparkles size={15} /> Random
                  </button>
                  <button
                    type="button"
                    className={universeMode === "established" ? "selected" : ""}
                    aria-pressed={universeMode === "established"}
                    onClick={() => chooseUniverseMode("established")}
                    disabled={learningState === "loading" || (!establishedAssets.length && !readyStockCatalog.length)}
                  >
                    <Brain size={15} /> Established Evidence
                  </button>
                </div>
                {universeMode === "established" ? (
                  <div className="evidenceGuidancePanel">
                    <div>
                      <strong>Evidence-guided</strong>
                      <span>{evidenceGuidance.assets.length ? `${evidenceGuidance.assets.length.toLocaleString()} retained assets` : learningState === "loading" ? "Loading learning snapshot" : "Using ready catalog until learning produces asset evidence"}</span>
                    </div>
                    <label>
                      <span>Evidence-guided allocation</span>
                      <strong>{evidenceAllocation}% Established / {100 - evidenceAllocation}% Exploration</strong>
                      <input
                        type="range"
                        min="50"
                        max="100"
                        step="5"
                        value={evidenceAllocation}
                        onChange={(event) => setEvidenceAllocation(Number(event.target.value))}
                      />
                    </label>
                    <small>Assets, strategy families, and timeframes come from the persisted Phase 9.9 learning snapshot. Stored validation thresholds stay unchanged.</small>
                  </div>
                ) : null}
                <div className="assetCountHeader"><label htmlFor="asset-count">Number of stock assets</label><span>{selectedStockCount.toLocaleString()} selected</span></div>
                <input
                  id="asset-count"
                  type="range"
                  min="1"
                  max={universeMode === "established" ? maxEstablishedSelection : maxStockSelection}
                  value={Math.max(1, selectedStockCount)}
                  onChange={(event) => chooseAssetCount(Number(event.target.value))}
                />
                <div className="assetCountInput">
                  <input
                    type="number"
                    min="1"
                    max={universeMode === "established" ? maxEstablishedSelection : maxStockSelection}
                    value={Math.max(1, selectedStockCount)}
                    onChange={(event) => chooseAssetCount(Number(event.target.value))}
                    aria-label="Number of stock assets"
                  />
                  <span>up to {(universeMode === "established" ? maxEstablishedSelection : maxStockSelection).toLocaleString()}</span>
                </div>
                <button
                  className="button secondary compact randomAssetButton"
                  type="button"
                  onClick={chooseRandomDefault}
                  disabled={!stockCatalog.length || universeMode === "established"}
                >
                  <Sparkles size={14} /> Random {maxReadyStockSelection}
                </button>
                <small className="assetCountHelp">
                  {universeMode === "established"
                    ? "Established Evidence uses the persisted learning ranking in stable order and does not randomly replace assets unless the learning evidence changes."
                    : `Random uses ready 1h/4h stocks first: ${readyStockCatalog.length.toLocaleString()} ready of ${stockCatalog.length.toLocaleString()} catalog stocks. It never uses only the ${visibleAssets.length.toLocaleString()} visible rows.`}
                </small>
              </div>
            )}

            <label className="assetSearch">
              <Search size={16} />
              <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search symbol or company" />
              <span>{visibleAssets.length} shown</span>
            </label>

            <div className="selectedAssetStrip" aria-label="Selected stocks">
              <EvidenceAssetGroups assets={selectedAssets} evidenceBySymbol={evidenceBySymbol} universeMode={universeMode} />
              {universeMode === "established" ? <span className="evidenceBadge"><Brain size={13} /> Evidence-guided</span> : null}
            </div>

            <div className="assetSelector" role="group" aria-label="Assets">
              {visibleAssets.map((asset) => {
                const selected = assetIds.includes(asset.id);
                const unavailable = !selected && assetIds.length >= MAX_PROFILE_ASSETS;
                const AssetIcon = asset.market === "Crypto" ? Bitcoin : asset.market === "ETF" ? Layers3 : TrendingUp;
                const evidence = evidenceBySymbol.get(asset.apiSymbol.toUpperCase());
                return (
                  <motion.button
                    key={asset.id}
                    type="button"
                    className={`assetOption ${selected ? "selected" : ""} ${universeMode === "established" && evidence ? `evidence-${evidence.tier}` : ""}`}
                    aria-pressed={selected}
                    disabled={unavailable}
                    onClick={() => toggleAsset(asset.id)}
                    whileHover={reduceMotion ? undefined : { y: -3 }}
                    whileTap={reduceMotion ? undefined : { scale: 0.98 }}
                  >
                    <span className="assetOptionTop"><AssetIcon size={16} /><span>{selected ? <Check size={13} /> : null}</span></span>
                    <strong>{asset.id}</strong>
                    <small>{asset.name}</small>
                    {universeMode === "established" && evidence ? (
                      <span className="assetEvidenceDetails">
                        <b>{starsForScore(evidence.score)}</b>
                        <em>Evidence Score {Math.round(evidence.score)}</em>
                        <em>Confidence {evidence.confidence}</em>
                        <em>{evidence.researchCandidates.toLocaleString()} candidates / {evidence.eliteCandidates.toLocaleString()} elite</em>
                        <em>{evidence.reasons.slice(0, 2).join(" / ")}</em>
                      </span>
                    ) : null}
                  </motion.button>
                );
              })}
            </div>
            {searchQuery && visibleAssets.length === 0 ? <p className="assetSearchEmpty">No Alpaca assets match this search.</p> : null}
            {universeMode === "established" && profileAsset && profileEvidence ? (
              <ResearchAssetProfile asset={profileAsset} evidence={profileEvidence} onClose={() => setProfileAssetId(null)} />
            ) : null}
          </div>

          <div className="builderPromise">
            <Sparkles size={19} />
            <div>
              <strong>What happens next</strong>
              <p>KefTrade observes the selected markets first, groups similar assets, and tests a 70/20/10 mix of strong-region, nearby, and exploratory variations. Weak ideas remain preserved as evidence.</p>
            </div>
          </div>
        </div>

        <aside className="campaignPreview" aria-label="Campaign preview">
          <div className="previewHeader">
            <span className="eyebrow">Campaign preview</span>
            <strong>Ready to research</strong>
          </div>

          <div className="previewSummary">
            <PreviewValue label="Research scope" value={selection.scopeLabel} />
            <PreviewValue label="Universe mode" value={universeMode === "established" ? "Evidence-guided" : "Random"} />
            <PreviewValue label="Scout evaluations" value={selection.scoutEstimatedJobs.toLocaleString()} mono />
            <PreviewValue label="Selected assets" value={selection.assets.length.toLocaleString()} mono />
            <PreviewValue label="Scout variations" value={selection.scoutCandidateCount.toLocaleString()} mono />
            <PreviewValue label="Expansion ceiling" value={selection.estimatedJobs.toLocaleString()} mono />
          </div>

          <div className="previewGroup">
            <span>Hypothesis families</span>
            <div className="strategyList">
              {(universeMode === "established" && evidenceGuidance.strategyFamilies.length ? evidenceGuidance.strategyFamilies : [...STRATEGY_FAMILIES]).map((strategy) => <span key={strategy}>{strategy}</span>)}
            </div>
          </div>

          <div className="previewGroup validationGroup">
            <span>Validation</span>
            {VALIDATION_METHODS.map((method) => <div key={method}><ShieldCheck size={15} /><strong>{method}</strong></div>)}
          </div>

          <motion.button
            type="button"
            className="researchLaunchButton"
            onClick={() => onLaunch(selection)}
            disabled={launching || selection.assets.length === 0 || catalogState === "loading"}
            whileHover={reduceMotion || launching ? undefined : { y: -3 }}
            whileTap={reduceMotion || launching ? undefined : { scale: 0.985 }}
          >
            <span><Sparkles size={18} /> {launching ? "Preparing campaign" : "Start Research Campaign"}</span>
            <ArrowRight size={19} />
          </motion.button>
          <p className="previewSafety">Research runs in simulation only. No live orders can be placed.</p>
        </aside>
      </div>
    </motion.section>
  );
}

function prioritizeAssets(assets: ResearchAsset[]) {
  const priority = new Map(preferredSymbols.map((symbol, index) => [symbol, index]));
  return [...assets].sort((left, right) => {
    const rightReady = readinessScore(right);
    const leftReady = readinessScore(left);
    if (rightReady !== leftReady) return rightReady - leftReady;
    const leftPriority = priority.get(left.id) ?? Number.MAX_SAFE_INTEGER;
    const rightPriority = priority.get(right.id) ?? Number.MAX_SAFE_INTEGER;
    return leftPriority - rightPriority || left.id.localeCompare(right.id);
  });
}

function boundedStockCount(rawCount: number, available: number) {
  return Math.max(1, Math.min(available, MAX_PROFILE_ASSETS, MAX_RANDOM_STOCKS, Math.floor(rawCount || 1)));
}

function randomStocks(assets: ResearchAsset[], count: number) {
  const pool = [...assets];
  for (let index = pool.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(Math.random() * (index + 1));
    [pool[index], pool[swapIndex]] = [pool[swapIndex], pool[index]];
  }
  return pool.slice(0, count);
}

function selectedFirst(assets: ResearchAsset[], selectedIds: ResearchAssetId[]) {
  const selected = new Set(selectedIds);
  return [...assets].sort((left, right) => {
    const leftSelected = selected.has(left.id) ? 1 : 0;
    const rightSelected = selected.has(right.id) ? 1 : 0;
    if (leftSelected !== rightSelected) return rightSelected - leftSelected;
    const leftOrder = selectedIds.indexOf(left.id);
    const rightOrder = selectedIds.indexOf(right.id);
    if (leftOrder !== -1 || rightOrder !== -1) return (leftOrder === -1 ? Number.MAX_SAFE_INTEGER : leftOrder) - (rightOrder === -1 ? Number.MAX_SAFE_INTEGER : rightOrder);
    return 0;
  });
}

function establishedGuidance(summary: ResearchLearningSummary | null, commandCenter: ResearchCommandCenter | null) {
  const global = summary?.global_learning;
  const priority = global?.campaign_guidance?.search_prioritization ?? {};
  const decision = global?.decision_intelligence ?? {};
  const proposal = commandCenter?.next_campaign_proposal ?? {};
  const retainedAssets = stringList(proposal.assets_to_retain);
  const deprioritizedAssets = stringList(proposal.assets_to_deprioritize);
  const decisionAssets = rankedNames(decision.assets, "priority_score");
  const decisionFamilies = rankedNames(decision.strategy_families, "priority_score");
  const decisionTimeframes = rankedNames(decision.timeframes, "priority_score");
  const assetRows = commandCenter?.asset_intelligence?.rows ?? [];
  const rowEvidence = assetRows.map((row) => assetEvidenceFromRow(row, retainedAssets, deprioritizedAssets));
  const retainedEvidence = retainedAssets.map((symbol) => rowEvidence.find((row) => row.symbol === symbol.toUpperCase()) ?? fallbackAssetEvidence(symbol, "established"));
  const deprioritizedEvidence = deprioritizedAssets.map((symbol) => rowEvidence.find((row) => row.symbol === symbol.toUpperCase()) ?? fallbackAssetEvidence(symbol, "deprioritized"));
  const remainingEvidence = rowEvidence.filter(
    (row) => !retainedAssets.some((symbol) => symbol.toUpperCase() === row.symbol) && !deprioritizedAssets.some((symbol) => symbol.toUpperCase() === row.symbol)
  );

  return {
    snapshotKey: global?.snapshot_key ?? null,
    assets: mergeStable(retainedAssets, mergeStable(priority.assets ?? [], decisionAssets)).filter(
      (symbol) => !deprioritizedAssets.some((deprioritized) => deprioritized.toUpperCase() === symbol.toUpperCase())
    ),
    deprioritizedAssets,
    assetEvidence: [...retainedEvidence, ...remainingEvidence, ...deprioritizedEvidence],
    strategyFamilies: mergeStable(priority.strategy_families ?? [], decisionFamilies),
    timeframes: mergeStable(priority.timeframes ?? [], decisionTimeframes)
  };
}

function assetEvidenceFromRow(row: Record<string, unknown>, retainedAssets: string[], deprioritizedAssets: string[]): AssetEvidence {
  const symbol = String(row.name ?? "").toUpperCase();
  const score = boundedScore(Number(row.candidate_quality_score ?? 0));
  const campaigns = Math.round(Number(row.campaign_count ?? 0));
  const validationRuns = Math.round(Number(row.validation_runs ?? 0));
  const researchCandidates = Math.round(Number(row.pass_rate ?? 0) * Number(row.validation_runs ?? 0));
  const tier = deprioritizedAssets.some((asset) => asset.toUpperCase() === symbol)
    ? "deprioritized"
    : retainedAssets.some((asset) => asset.toUpperCase() === symbol) || score >= 40
      ? "established"
      : "exploration";
  const reasons = [
    score >= 40 ? "Positive historical quality" : null,
    Number(row.average_profit_factor ?? 0) > 0 ? `PF ${formatMetric(row.average_profit_factor)}` : null,
    Number(row.average_expectancy ?? 0) > 0 ? `Expectancy ${formatMetric(row.average_expectancy)}` : null,
    Number(row.median_trade_count ?? 0) > 0 ? `${Math.round(Number(row.median_trade_count))} median trades` : null,
    tier === "deprioritized" ? readableFailure(row.dominant_failure_reason) : null
  ].filter((reason): reason is string => Boolean(reason));

  return {
    symbol,
    tier,
    score,
    confidence: confidenceForEvidence(campaigns, validationRuns, researchCandidates),
    campaigns,
    validationRuns,
    researchCandidates,
    eliteCandidates: Number(row.elite_candidates ?? 0),
    forwardDeployments: Number(row.forward_deployments ?? 0),
    profitFactor: Number(row.average_profit_factor ?? 0),
    expectancy: Number(row.average_expectancy ?? 0),
    tradeCount: Number(row.median_trade_count ?? 0),
    drawdown: Number(row.median_drawdown ?? 0),
    bestFamily: typeof row.best_strategy_family === "string" ? row.best_strategy_family : null,
    bestTimeframe: typeof row.best_timeframe === "string" ? row.best_timeframe : null,
    dominantFailure: typeof row.dominant_failure_reason === "string" ? row.dominant_failure_reason : null,
    repairSuccess: typeof row.repair_success_rate === "number" ? row.repair_success_rate : null,
    reasons: reasons.length ? reasons : [tier === "deprioritized" ? "Repeated historical failures" : "Selected for controlled exploration"]
  };
}

function fallbackAssetEvidence(symbol: string, tier: EvidenceTier): AssetEvidence {
  return {
    symbol: symbol.toUpperCase(),
    tier,
    score: tier === "established" ? 60 : tier === "deprioritized" ? 15 : 30,
    confidence: "Low",
    campaigns: 0,
    validationRuns: 0,
    researchCandidates: 0,
    eliteCandidates: 0,
    forwardDeployments: 0,
    profitFactor: 0,
    expectancy: 0,
    tradeCount: 0,
    drawdown: 0,
    bestFamily: null,
    bestTimeframe: null,
    dominantFailure: null,
    repairSuccess: null,
    reasons: [tier === "established" ? "Retained by latest campaign proposal" : tier === "deprioritized" ? "Deprioritized by latest campaign proposal" : "Selected for controlled exploration"]
  };
}

function rankedNames(rows: Array<Record<string, unknown>> | undefined, scoreField: string) {
  return [...(rows ?? [])]
    .filter((row) => Number(row[scoreField] ?? 0) > 0)
    .sort((left, right) => {
      const scoreDelta = Number(right[scoreField] ?? 0) - Number(left[scoreField] ?? 0);
      const testedDelta = Number(right.tested ?? right.validation_runs ?? 0) - Number(left.tested ?? left.validation_runs ?? 0);
      return scoreDelta || testedDelta || String(left.name ?? "").localeCompare(String(right.name ?? ""));
    })
    .map((row) => String(row.name ?? ""))
    .filter(Boolean);
}

function mergeStable(first: string[], second: string[]) {
  const seen = new Set<string>();
  const merged: string[] = [];
  for (const value of [...first, ...second]) {
    const normalized = String(value);
    const key = normalized.toUpperCase();
    if (!normalized || seen.has(key)) continue;
    seen.add(key);
    merged.push(normalized);
  }
  return merged;
}

function learnedAssets(stockCatalog: ResearchAsset[], rankedSymbols: string[], deprioritizedSymbols: string[], evidenceAllocationPct: number) {
  const bySymbol = new Map(stockCatalog.map((asset) => [asset.apiSymbol.toUpperCase(), asset]));
  const retainedAssets = rankedSymbols
    .map((symbol) => bySymbol.get(symbol.toUpperCase()))
    .filter((asset): asset is ResearchAsset => Boolean(asset));
  const retained = retainedAssets.filter(isResearchReadyStock);
  const retainedPool = retained.length ? retained : retainedAssets;
  const retainedSymbols = new Set(retainedPool.map((asset) => asset.apiSymbol.toUpperCase()));
  const deprioritizedSet = new Set(deprioritizedSymbols.map((symbol) => symbol.toUpperCase()));
  const explorationPool = prioritizeAssets(stockCatalog).filter((asset) => !retainedSymbols.has(asset.apiSymbol.toUpperCase()) && !deprioritizedSet.has(asset.apiSymbol.toUpperCase()));
  const deprioritizedPool = deprioritizedSymbols
    .map((symbol) => bySymbol.get(symbol.toUpperCase()))
    .filter((asset): asset is ResearchAsset => Boolean(asset));
  const evidenceSlots = Math.max(1, Math.round(MAX_RANDOM_STOCKS * (evidenceAllocationPct / 100)));
  return [...retainedPool.slice(0, evidenceSlots), ...explorationPool, ...retainedPool.slice(evidenceSlots), ...deprioritizedPool];
}

function stringList(value: unknown) {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function boundedScore(value: number) {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function readableFailure(value: unknown) {
  const raw = String(value ?? "").replaceAll("_", " ");
  return raw && raw !== "aggregate view" ? raw : "Repeated historical failures";
}

function formatMetric(value: unknown) {
  const number = Number(value ?? 0);
  return Number.isFinite(number) ? number.toFixed(number >= 10 ? 0 : 2) : "0";
}

function confidenceForEvidence(campaigns: number, validationRuns: number, researchCandidates: number): "High" | "Medium" | "Low" {
  if (campaigns >= 10 && validationRuns >= 200 && researchCandidates >= 5) return "High";
  if (campaigns >= 4 && validationRuns >= 60 && researchCandidates >= 1) return "Medium";
  return "Low";
}

function starsForScore(score: number) {
  const filled = Math.max(1, Math.min(5, Math.round(score / 20)));
  return `${"★".repeat(filled)}${"☆".repeat(5 - filled)}`;
}

function readinessScore(asset: ResearchAsset) {
  const candleReady = Math.min(asset.ready1hCandles ?? 0, 120) + Math.min(asset.ready4hCandles ?? 0, 120);
  const featureReady = Math.min(asset.ready1hFeatures ?? 0, 80) + Math.min(asset.ready4hFeatures ?? 0, 80);
  return candleReady + featureReady + (asset.researchReady ? 1000 : 0);
}

function isResearchReadyStock(asset: ResearchAsset) {
  if (asset.researchReady !== undefined) return asset.researchReady;
  return (
    (asset.ready1hCandles ?? 0) >= 120 &&
    (asset.ready4hCandles ?? 0) >= 120 &&
    (asset.ready1hFeatures ?? 0) >= 80 &&
    (asset.ready4hFeatures ?? 0) >= 80
  );
}

function EvidenceAssetGroups({
  assets,
  evidenceBySymbol,
  universeMode
}: {
  assets: ResearchAsset[];
  evidenceBySymbol: Map<string, AssetEvidence>;
  universeMode: "random" | "established";
}) {
  if (universeMode !== "established") {
    return (
      <div>
        <strong>{assets.length.toLocaleString()} chosen</strong>
        <small>{assets.map((asset) => asset.id).join(", ")}</small>
      </div>
    );
  }

  const groups: Array<{ tier: EvidenceTier; label: string; assets: ResearchAsset[] }> = [
    { tier: "established", label: "Established", assets: [] },
    { tier: "exploration", label: "Exploration", assets: [] },
    { tier: "deprioritized", label: "Deprioritized", assets: [] }
  ];
  const byTier = new Map(groups.map((group) => [group.tier, group]));
  for (const asset of assets) {
    const evidence = evidenceBySymbol.get(asset.apiSymbol.toUpperCase());
    byTier.get(evidence?.tier ?? "exploration")?.assets.push(asset);
  }

  return (
    <div className="selectedEvidenceGroups">
      <strong>{assets.length.toLocaleString()} chosen</strong>
      {groups.filter((group) => group.assets.length).map((group) => (
        <section key={group.tier} className={`selectedEvidenceGroup ${group.tier}`}>
          <span>{group.label} / {group.assets.length.toLocaleString()}</span>
          <small>{group.assets.map((asset) => asset.id).join(", ")}</small>
        </section>
      ))}
    </div>
  );
}

function ResearchAssetProfile({ asset, evidence, onClose }: { asset: ResearchAsset; evidence: AssetEvidence; onClose: () => void }) {
  return (
    <aside className="researchAssetProfile" aria-label={`${asset.id} research profile`}>
      <div className="researchAssetProfileHeader">
        <div>
          <span>Research Profile</span>
          <strong>{asset.id}</strong>
          <small>{asset.name}</small>
        </div>
        <button type="button" onClick={onClose} aria-label="Close research profile">Close</button>
      </div>

      <div className="profileScoreGrid">
        <ProfileMetric label="Evidence Score" value={String(Math.round(evidence.score))} />
        <ProfileMetric label="Confidence" value={evidence.confidence} />
        <ProfileMetric label="Campaigns" value={evidence.campaigns.toLocaleString()} />
        <ProfileMetric label="Validation Runs" value={evidence.validationRuns.toLocaleString()} />
      </div>

      <div className="profileBasedOn">
        <span>Based on</span>
        <small>{evidence.campaigns.toLocaleString()} campaigns</small>
        <small>{evidence.validationRuns.toLocaleString()} validation runs</small>
        <small>{evidence.forwardDeployments.toLocaleString()} forward deployments</small>
      </div>

      <div className="profileScoreGrid">
        <ProfileMetric label="Research Candidates" value={evidence.researchCandidates.toLocaleString()} />
        <ProfileMetric label="Elite" value={evidence.eliteCandidates.toLocaleString()} />
        <ProfileMetric label="Forward" value={evidence.forwardDeployments.toLocaleString()} />
        <ProfileMetric label="Best Timeframe" value={evidence.bestTimeframe ?? "Not measured"} />
        <ProfileMetric label="Best Family" value={evidence.bestFamily ?? "Not measured"} />
        <ProfileMetric label="Most Common Failure" value={evidence.dominantFailure ? readableFailure(evidence.dominantFailure) : "None observed"} />
        <ProfileMetric label="Repair Success" value={evidence.repairSuccess == null ? "Not measured" : `${Math.round(evidence.repairSuccess * 100)}%`} />
        <ProfileMetric label="Expected PF" value={formatMetric(evidence.profitFactor)} />
        <ProfileMetric label="Expected Expectancy" value={formatMetric(evidence.expectancy)} />
      </div>
    </aside>
  );
}

function ProfileMetric({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function PreviewValue({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div><span>{label}</span><strong className={mono ? "monoValue" : undefined}>{value}</strong></div>;
}
