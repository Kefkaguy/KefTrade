"use client";

import { useMemo, useState } from "react";
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
      await syncCandles({ symbol: selectedAsset, timeframe: profile.timeframe, provider: profile.provider });
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
    <div className="simpleFlow">
      <section className="analysisHero">
        <div>
          <h1>KefTrade</h1>
          <p>Evidence-based market research</p>
        </div>
        <div className="researchOnly">Research only. No buy/sell recommendations.</div>
      </section>

      <section className="analysisCard" aria-label="Start market research">
        <div className="analysisForm">
          <div>
            <span className="sectionLabel">Search or select asset</span>
            <div className="assetPicker">
              {ASSETS.map((item) => (
                <button key={item} className={selectedAsset === item ? "selected" : ""} type="button" onClick={() => { setAsset(item); setCustomAsset(""); }}>
                  {item}
                </button>
              ))}
            </div>
            <input className="largeInput" value={customAsset} onChange={(event) => setCustomAsset(event.target.value)} placeholder="Or type a supported symbol" />
          </div>

          <label className="field">
            <span className="sectionLabel">Strategy</span>
            <select value={strategy} onChange={(event) => setStrategy(event.target.value)}>
              {STRATEGIES.map((item) => (
                <option key={item.label} value={item.value}>{item.label}</option>
              ))}
            </select>
          </label>

          <button className="analyzeButton" type="button" onClick={analyze} disabled={loading || !selectedAsset}>
            {loading ? `Researching ${selectedAsset}...` : "Analyze"}
          </button>
        </div>
      </section>

      {loading ? <LoadingState asset={selectedAsset} stepIndex={stepIndex} /> : null}
      <Toast tone={toast.tone} message={toast.message} />
      {result ? (
        <ResearchAnswer
          result={result}
          onSave={saveReport}
          onDownload={downloadReport}
          onDismiss={() => {
            setResult(null);
            setToast({ tone: "info", message: "" });
          }}
          onNew={() => {
            setResult(null);
            window.scrollTo({ top: 0, behavior: "smooth" });
          }}
        />
      ) : !loading ? (
        <section className="friendlyEmpty">
          <strong>Start with one asset.</strong>
          <p>Pick TSLA, NVDA, SPY, BTCUSDT, or another supported symbol. KefTrade will load data, run evidence checks, and explain the result in plain language.</p>
        </section>
      ) : null}
    </div>
  );
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
    provider: isCrypto ? "binance_dev" : "yfinance_research",
    timeframe: isCrypto ? "4h" : "1d"
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
