"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowUpRight, DatabaseZap, ScanSearch, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { ResearchConstellation } from "@/components/ResearchConstellation";
import { EvidenceBadges, Toast } from "@/components/ResearchUI";
import {
  askCopilot,
  runStrategyResearch,
  syncCandles,
  syncFeatures,
  type CopilotResponse,
  type StrategyResearchReport,
  type StrategyResearchRun
} from "@/lib/api";
import { number, percent } from "@/lib/format";

const ASSETS = ["TSLA", "NVDA", "AAPL", "MSFT", "SPY", "QQQ", "BTCUSDT", "ETHUSDT"];

const STRATEGIES = [
  { label: "Auto Research", value: "" },
  { label: "Trend Pullback", value: "trend_pullback" },
  { label: "Breakout", value: "breakout" },
  { label: "Momentum", value: "momentum" },
  { label: "Mean Reversion", value: "mean_reversion" },
  { label: "Volatility Breakout", value: "volatility_breakout" }
];

const LOADING_STEPS = [
  "Loading market data",
  "Running strategies",
  "Validating evidence",
  "Checking market regimes",
  "Asking AI Copilot to summarize results"
];

type Verdict = "Strong Evidence" | "Needs More Research" | "Weak Evidence" | "Rejected";

type SavedReport = {
  id: string;
  createdAt: string;
  asset: string;
  strategy: string;
  verdict: Verdict;
  confidence: string;
  summary: string;
  evidenceRefs: string[];
  markdown: string;
};

type AnalysisResult = {
  asset: string;
  strategy: string;
  timeframe: string;
  verdict: Verdict;
  confidence: string;
  reasons: string[];
  nextStep: string;
  evidenceRefs: string[];
  ai: CopilotResponse | null;
  report: StrategyResearchReport;
  topRun: StrategyResearchRun | null;
};

export function SimpleResearchFlow() {
  const reduceMotion = useReducedMotion();
  const [asset, setAsset] = useState("TSLA");
  const [strategy, setStrategy] = useState("");
  const [customAsset, setCustomAsset] = useState("");
  const [loading, setLoading] = useState(false);
  const [stepIndex, setStepIndex] = useState(-1);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });

  const selectedAsset = (customAsset.trim() || asset).toUpperCase();
  const selectedStrategy = STRATEGIES.find((item) => item.value === strategy)?.label ?? "Auto Research";

  async function analyze() {
    setLoading(true);
    setResult(null);
    setToast({ tone: "info", message: "" });
    setStepIndex(0);

    const profile = assetProfile(selectedAsset);
    try {
      await syncCandles({ symbol: selectedAsset, timeframe: profile.timeframe, provider: profile.provider, limit: profile.limit });
      setStepIndex(1);
      await syncFeatures({ symbol: selectedAsset, timeframe: profile.timeframe });
      setStepIndex(2);
      const report = await runStrategyResearch({ symbol: selectedAsset, timeframe: profile.timeframe, strategy: strategy || undefined });
      setStepIndex(3);
      const topRun = report.ranking_table?.[0] ?? null;
      const evidenceRefs = buildEvidenceRefs(report, topRun);
      const verdict = mapVerdict(topRun?.recommendation);
      const reasons = buildReasons(topRun);
      setStepIndex(4);
      const ai = await summarizeWithCopilot(selectedAsset, selectedStrategy, verdict, reasons, evidenceRefs);

      setResult({
        asset: selectedAsset,
        strategy: selectedStrategy,
        timeframe: profile.timeframe,
        verdict,
        confidence: ai?.confidence ?? confidenceFromRun(topRun),
        reasons,
        nextStep: nextResearchStep(verdict),
        evidenceRefs,
        ai,
        report,
        topRun
      });
      setToast({ tone: "success", message: "Analysis complete. The answer below is research-only and cites stored evidence when available." });
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? `Analysis stopped: ${error.message}` : "Analysis stopped. Check backend data and try again." });
    } finally {
      setLoading(false);
      setStepIndex(-1);
    }
  }

  function saveReport() {
    if (!result) return;
    const saved = toSavedReport(result);
    const existing = readReports();
    window.localStorage.setItem("keftrade-saved-reports", JSON.stringify([saved, ...existing].slice(0, 50)));
    setToast({ tone: "success", message: "Report saved in this browser." });
  }

  function downloadReport() {
    if (!result) return;
    const saved = toSavedReport(result);
    const blob = new Blob([saved.markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${result.asset.toLowerCase()}-research-report.md`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="simpleFlow commandHome">
      <section className="commandHero">
        <motion.div className="commandIntro" initial={reduceMotion ? false : { opacity: 0, y: 24 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.65, ease: [0.22, 1, 0.36, 1] }}>
          <div className="heroKicker"><span className="liveDot" /> Quantitative research intelligence</div>
          <h1><span>Research the market.</span><em>Remove the noise.</em></h1>
          <p>KefTrade turns raw market history into tested, traceable evidence—before a strategy gets anywhere near execution.</p>
          <div className="heroFootnotes"><span><ShieldCheck size={14} /> Simulation-only</span><span><DatabaseZap size={14} /> Source-backed</span><span><ScanSearch size={14} /> Deterministic</span></div>
        </motion.div>
        <motion.div className="constellationWrap" initial={reduceMotion ? false : { opacity: 0, scale: 0.94 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: 0.75, delay: 0.12 }}><ResearchConstellation /></motion.div>
      </section>

      <motion.section className="researchComposer" aria-label="Start market research" initial={reduceMotion ? false : { opacity: 0, y: 28 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6, delay: 0.2, ease: [0.22, 1, 0.36, 1] }}>
        <header className="composerHeader"><div><span>01</span><strong>Compose a research run</strong></div><p>Choose the market. KefTrade handles the evidence chain.</p></header>
        <div className="composerBody">
          <div className="assetComposer">
            <label htmlFor="asset-command">Asset universe</label>
            <div className="assetPicker">{ASSETS.map((item) => <motion.button key={item} className={selectedAsset === item ? "selected" : ""} type="button" onClick={() => { setAsset(item); setCustomAsset(""); }} whileHover={reduceMotion ? undefined : { y: -2 }} whileTap={reduceMotion ? undefined : { scale: 0.96 }}><span>{item}</span></motion.button>)}</div>
            <div className="commandInputWrap"><span className="commandPrompt">KT:</span><input id="asset-command" className="largeInput" value={customAsset} onChange={(event) => setCustomAsset(event.target.value)} placeholder="Type any supported symbol" /><span className="commandCursor" /></div>
          </div>
          <label className="strategyComposer"><span>Research protocol</span><select value={strategy} onChange={(event) => setStrategy(event.target.value)}>{STRATEGIES.map((item) => <option key={item.label} value={item.value}>{item.label}</option>)}</select></label>
          <motion.button className="analyzeButton" type="button" onClick={analyze} disabled={loading || !selectedAsset} whileHover={reduceMotion || loading ? undefined : { scale: 1.025 }} whileTap={reduceMotion || loading ? undefined : { scale: 0.98 }}><span>{loading ? `Running ${selectedAsset}` : "Run evidence"}</span>{loading ? <span className="buttonLoader" /> : <ArrowUpRight size={18} />}</motion.button>
        </div>
        <footer className="composerFooter"><span>Provider <strong>{assetProfile(selectedAsset).provider}</strong></span><span>Timeframe <strong>{assetProfile(selectedAsset).timeframe}</strong></span><span>Mode <strong>Research only</strong></span></footer>
      </motion.section>

      <div className="marketMarquee" aria-hidden="true"><div>{[...ASSETS, ...ASSETS].map((item, index) => <span key={`${item}-${index}`}><i />{item}<small>evidence ready</small></span>)}</div></div>
      <AnimatePresence mode="wait">
        {loading ? <motion.div key="loading" initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}><LoadingState asset={selectedAsset} stepIndex={stepIndex} /></motion.div> : null}
        {!loading && result ? <motion.div key="result" initial={{ opacity: 0, y: 24 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -12 }}><ResearchAnswer result={result} onSave={saveReport} onDownload={downloadReport} onDismiss={() => { setResult(null); setToast({ tone: "info", message: "" }); }} onNew={() => { setResult(null); window.scrollTo({ top: 0, behavior: "smooth" }); }} /></motion.div> : null}
        {!loading && !result ? <ResearchProcess key="process" /> : null}
      </AnimatePresence>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}

function ResearchProcess() {
  const steps = [
    { index: "01", icon: DatabaseZap, title: "Ingest", body: "Normalize candle history and technical features from the selected provider." },
    { index: "02", icon: ScanSearch, title: "Challenge", body: "Run deterministic strategies, backtests, regimes, and evidence gates." },
    { index: "03", icon: ShieldCheck, title: "Explain", body: "Turn the result into a traceable verdict with cited research evidence." }
  ];
  return <motion.section className="researchProcess" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.3 }}><header><span className="sectionLabel">Research architecture</span><h2>From market history to a defensible answer.</h2></header><div>{steps.map((step) => { const Icon = step.icon; return <article key={step.index}><span className="processIndex">{step.index}</span><Icon size={22} /><h3>{step.title}</h3><p>{step.body}</p><span className="processLine" /></article>; })}</div></motion.section>;
}

function LoadingState({ asset, stepIndex }: { asset: string; stepIndex: number }) {
  return (
    <section className="analysisCard">
      <div className="loadingResearch">
        <h2>Researching {asset}...</h2>
        <div>
          {LOADING_STEPS.map((step, index) => (
            <span key={step} className={index <= stepIndex ? "complete" : ""}>
              {step}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

function ResearchAnswer({ result, onSave, onDownload, onDismiss, onNew }: { result: AnalysisResult; onSave: () => void; onDownload: () => void; onDismiss: () => void; onNew: () => void }) {
  const metrics = result.topRun?.metrics ?? {};
  const regimeRows = result.topRun?.by_market_regime ?? [];
  const summary = result.ai?.answer || plainSummary(result);

  return (
    <section className="resultPanel">
      <div className="resultHeader">
        <div>
          <span className="sectionLabel">Asset</span>
          <h2>{result.asset}</h2>
        </div>
        <div className={`verdict ${verdictClass(result.verdict)}`}>{result.verdict}</div>
      </div>

      <div className="answerGrid">
        <div className="answerMain">
          <span className="sectionLabel">Research Verdict</span>
          <h3>{result.verdict}</h3>
          <p>{summary}</p>
          <div className="reasonList">
            {result.reasons.map((reason) => <span key={reason}>{reason}</span>)}
          </div>
        </div>
        <div className="answerSide">
          <span>Confidence <strong>{result.confidence}</strong></span>
          <span>Strategy <strong>{result.strategy}</strong></span>
          <span>Timeframe <strong>{result.timeframe}</strong></span>
        </div>
      </div>

      <div className="resultSection">
        <span className="sectionLabel">Evidence references</span>
        <EvidenceBadges refs={result.evidenceRefs} />
      </div>

      <div className="resultSection">
        <span className="sectionLabel">Next recommended research step</span>
        <p>{result.nextStep}</p>
      </div>

      <div className="resultActions">
        <button className="button" type="button" onClick={onSave}>Save Report</button>
        <button className="button secondary" type="button" onClick={onDownload}>Download Report</button>
        <button className="button ghost" type="button" onClick={onNew}>New Analysis</button>
        <button className="button ghost" type="button" onClick={onDismiss}>Dismiss</button>
      </div>

      <details className="technicalDetails">
        <summary>View Technical Details</summary>
        <div className="technicalGrid">
          <Metric label="Profit factor" value={number(metrics.profit_factor)} />
          <Metric label="Expectancy" value={number(metrics.expectancy_per_trade)} />
          <Metric label="Max drawdown" value={percent(metrics.max_drawdown)} />
          <Metric label="Trade count" value={String(metrics.number_of_trades ?? result.topRun?.trade_count ?? "N/A")} />
          <Metric label="Stability score" value={number(result.topRun?.rank_score)} />
          <Metric label="Confidence interval" value={confidenceInterval(result.topRun)} />
          <Metric label="Validation runs" value={String(result.report.run_count ?? 0)} />
          <Metric label="Strategy metrics" value={result.report.rank_metrics?.join(", ") || "N/A"} />
        </div>
        <div className="technicalBlock">
          <h3>Regime breakdown</h3>
          {regimeRows.length ? regimeRows.map((row) => <p key={row.regime}>{row.regime}: profit factor {number(row.metrics.profit_factor)}, trades {String(row.metrics.number_of_trades ?? "N/A")}</p>) : <p>No regime breakdown returned.</p>}
        </div>
        <div className="technicalBlock">
          <h3>Evidence rules</h3>
          <ul>
            {(result.topRun?.entry_rules ?? []).map((rule) => <li key={rule}>{rule}</li>)}
            {(result.topRun?.exit_rules ?? []).map((rule) => <li key={rule}>{rule}</li>)}
          </ul>
        </div>
      </details>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function assetProfile(symbol: string) {
  const isCrypto = symbol.endsWith("USDT");
  return {
    provider: isCrypto ? "binance_dev" : "alpaca_iex",
    timeframe: isCrypto ? "4h" : "1h",
    limit: isCrypto ? 1500 : 5000
  };
}

async function summarizeWithCopilot(asset: string, strategy: string, verdict: Verdict, reasons: string[], evidenceRefs: string[]) {
  try {
    return await askCopilot(
      `Summarize this KefTrade research result for ${asset}. Strategy: ${strategy}. Verdict label: ${verdict}. Reasons: ${reasons.join("; ")}. Evidence refs: ${evidenceRefs.join(", ") || "none"}. Use simple language, stay read-only, cite evidence references, and refuse buy/sell or trading-action wording.`
    );
  } catch {
    return null;
  }
}

function mapVerdict(recommendation: string | undefined): Verdict {
  if (recommendation === "Candidate for Paper Trading" || recommendation === "Validated Alpha") return "Strong Evidence";
  if (recommendation === "Needs More Research" || recommendation === "Research More") return "Needs More Research";
  if (recommendation === "Reject") return "Rejected";
  return "Weak Evidence";
}

function verdictClass(verdict: Verdict) {
  if (verdict === "Strong Evidence") return "strong";
  if (verdict === "Needs More Research") return "needs";
  if (verdict === "Rejected") return "rejected";
  return "weak";
}

function confidenceFromRun(run: StrategyResearchRun | null) {
  if (!run) return "Insufficient evidence";
  if (run.rank_score >= 0.75) return "High";
  if (run.rank_score >= 0.45) return "Medium";
  return "Low";
}

function buildEvidenceRefs(report: StrategyResearchReport, run: StrategyResearchRun | null) {
  const refs = [];
  if (run?.run_id) refs.push(`strategy_run:${run.run_id}`);
  if (report.symbol) refs.push(`asset:${report.symbol}`);
  return refs;
}

function buildReasons(run: StrategyResearchRun | null) {
  if (!run) return ["No strategy result was returned by the backend."];
  const metrics = run.metrics ?? {};
  const reasons = [];
  const pf = Number(metrics.profit_factor);
  const trades = Number(metrics.number_of_trades ?? run.trade_count);
  const drawdown = Number(metrics.max_drawdown);
  if (Number.isFinite(pf)) reasons.push(`Profit factor: ${number(pf)}`);
  if (Number.isFinite(trades)) reasons.push(`Trade count: ${trades}`);
  if (Number.isFinite(drawdown)) reasons.push(`Max drawdown: ${percent(drawdown)}`);
  reasons.push(`Backend recommendation: ${run.recommendation}`);
  return reasons;
}

function nextResearchStep(verdict: Verdict) {
  if (verdict === "Strong Evidence") return "Run broader validation across related assets and timeframes before trusting the evidence.";
  if (verdict === "Needs More Research") return "Increase sample size, compare another strategy, and inspect failed evidence rules.";
  if (verdict === "Rejected") return "Try a different strategy or asset, then compare why this evidence failed.";
  return "Sync more data and run another evidence check before drawing conclusions.";
}

function plainSummary(result: AnalysisResult) {
  return `KefTrade tested ${result.strategy} against historical ${result.asset} data. The current evidence is ${result.verdict.toLowerCase()} based on ${result.reasons.join(", ")}.`;
}

function confidenceInterval(run: StrategyResearchRun | null) {
  const mc = run?.metrics?.confidence_interval ?? run?.metrics?.expectancy_confidence_interval;
  if (Array.isArray(mc)) return mc.map((item) => number(item)).join(" to ");
  return "N/A";
}

function toSavedReport(result: AnalysisResult): SavedReport {
  const summary = result.ai?.answer || plainSummary(result);
  const markdown = [
    `# KefTrade Research Report: ${result.asset}`,
    "",
    `Strategy: ${result.strategy}`,
    `Verdict: ${result.verdict}`,
    `Confidence: ${result.confidence}`,
    "",
    "## Summary",
    summary,
    "",
    "## Main Reasons",
    ...result.reasons.map((reason) => `- ${reason}`),
    "",
    "## Evidence References",
    ...(result.evidenceRefs.length ? result.evidenceRefs.map((ref) => `- ${ref}`) : ["- No evidence references returned."]),
    "",
    "## Next Research Step",
    result.nextStep,
    "",
    "Research-only output. This report does not recommend buying, selling, or taking trading action."
  ].join("\n");
  return {
    id: `${Date.now()}-${result.asset}`,
    createdAt: new Date().toISOString(),
    asset: result.asset,
    strategy: result.strategy,
    verdict: result.verdict,
    confidence: result.confidence,
    summary,
    evidenceRefs: result.evidenceRefs,
    markdown
  };
}

function readReports(): SavedReport[] {
  try {
    return JSON.parse(window.localStorage.getItem("keftrade-saved-reports") || "[]") as SavedReport[];
  } catch {
    return [];
  }
}
