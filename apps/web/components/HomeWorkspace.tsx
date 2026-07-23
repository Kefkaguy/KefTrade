"use client";

import Image from "next/image";
import Link from "next/link";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowDown, ArrowRight, Check, Search, ShieldCheck, Sparkles } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { CampaignActivity } from "@/components/CampaignActivity";
import { StrategyLibraryPanel } from "@/components/StrategyLibraryPanel";
import { ResearchBuilder } from "@/components/ResearchBuilder";
import { ResearchLaunchExperience, type ResearchLaunchPhase } from "@/components/ResearchLaunchExperience";
import {
  createResearchCampaign,
  getResearchCampaign,
  prepareResearchCampaign,
  preflightResearchCampaign,
  runParallelResearchCampaign,
  saveResearchUniverse,
  type MissionControlSnapshot,
  type ResearchCampaignCreateResult,
  type ResearchCampaignPreflight,
  type ResearchCampaignStatus
} from "@/lib/api";
import { RESEARCH_TIMEFRAMES, buildResearchSelection, researchUniverseKey, type ResearchSelection } from "@/lib/home-research";

type HomeWorkspaceProps = {
  snapshot: MissionControlSnapshot | null;
  error?: string | null;
};

const journey = [
  { number: "01", title: "Observe the market", detail: "Freeze the dataset, measure each asset, and group similar behavior." },
  { number: "02", title: "Test a hypothesis", detail: "Generate focused 70/20/10 strategy variations around measured evidence." },
  { number: "03", title: "Learn and preserve", detail: "Classify specialists and elites, record failures, and archive the experiment." }
] as const;

export function HomeWorkspace({ snapshot, error: serviceError }: HomeWorkspaceProps) {
  const reduceMotion = useReducedMotion();
  const [phase, setPhase] = useState<"idle" | ResearchLaunchPhase>("idle");
  const [selection, setSelection] = useState<ResearchSelection | null>(null);
  const [createdCampaign, setCreatedCampaign] = useState<ResearchCampaignCreateResult | null>(null);
  const [status, setStatus] = useState<ResearchCampaignStatus | null>(null);
  const [launchError, setLaunchError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  const runGenerationRef = useRef(0);
  const pollingRef = useRef(false);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      runGenerationRef.current += 1;
    };
  }, []);

  const processCampaign = useCallback(async (campaignId: number, generation: number) => {
    const startedAt = now();
    logLaunchDiagnostic("Parallel launch started", { campaignId });
    try {
      await runParallelResearchCampaign(campaignId, 4, 25);
      if (!aliveRef.current || generation !== runGenerationRef.current) return;
      logLaunchDiagnostic("Parallel launch finished", { campaignId, elapsedMs: elapsedSince(startedAt) });
      const nextStatus = await getResearchCampaign(campaignId);
      if (!aliveRef.current || generation !== runGenerationRef.current) return;
      setStatus(nextStatus);
      if (nextStatus.campaign.status === "completed") setPhase("complete");
      else setPhase("running");
    } catch (error) {
      if (!aliveRef.current || generation !== runGenerationRef.current) return;
      logLaunchException("Parallel launch exception", error, { campaignId, elapsedMs: elapsedSince(startedAt) });
      setLaunchError(readError(error));
      setPhase("error");
    }
  }, []);

  useEffect(() => {
    const campaignId = createdCampaign?.campaign.id;
    if (phase !== "running" || !campaignId) return;

    const timer = window.setInterval(async () => {
      if (pollingRef.current || !aliveRef.current) return;
      pollingRef.current = true;
      const pollStartedAt = now();
      logLaunchDiagnostic("Polling started", { campaignId });
      try {
        const nextStatus = await getResearchCampaign(campaignId);
        if (!aliveRef.current) return;
        setStatus(nextStatus);
        if (nextStatus.campaign.status === "completed") setPhase("complete");
        logLaunchDiagnostic("Polling finished", { campaignId, status: nextStatus.campaign.status, elapsedMs: elapsedSince(pollStartedAt) });
        const blockedJobs = Number(nextStatus.analytics.jobs_by_status?.blocked_data ?? 0);
        if (blockedJobs > 0) {
          setLaunchError(`${blockedJobs.toLocaleString()} jobs were blocked because required market data or features are unavailable. The remaining queue has been stopped.`);
          setPhase("error");
        }
        if (["paused", "failed", "canceled"].includes(nextStatus.campaign.status.toLowerCase())) {
          setLaunchError(`Campaign ${campaignId} is ${nextStatus.campaign.status.toLowerCase()}. No additional jobs will be started.`);
          setPhase("error");
        }
      } catch (error) {
        logLaunchException("Polling timeout or failure", error, { campaignId, elapsedMs: elapsedSince(pollStartedAt) });
        // The active execution request can briefly hold the local API; the next poll retries.
      } finally {
        pollingRef.current = false;
      }
    }, 5000);

    return () => window.clearInterval(timer);
  }, [createdCampaign?.campaign.id, phase]);

  async function launchCampaign(nextSelection: ResearchSelection) {
    const launchStartedAt = now();
    const generation = runGenerationRef.current + 1;
    runGenerationRef.current = generation;
    setSelection(nextSelection);
    setCreatedCampaign(null);
    setStatus(null);
    setLaunchError(null);
    setPhase("creating");
    logLaunchDiagnostic("Launch button clicked", { assets: nextSelection.assets.map((asset) => asset.apiSymbol), candidateCount: nextSelection.candidateCount, estimatedJobs: nextSelection.estimatedJobs });

    try {
      const selectedTimeframes = nextSelection.timeframes?.length ? nextSelection.timeframes : [...RESEARCH_TIMEFRAMES];
      const preflightStartedAt = now();
      logLaunchDiagnostic("Preflight started", { assets: nextSelection.assets.length, timeframes: selectedTimeframes });
      let preflight = await preflightResearchCampaign(
        nextSelection.assets.map((asset) => asset.apiSymbol),
        selectedTimeframes
      );
      if (!aliveRef.current || generation !== runGenerationRef.current) return;
      logLaunchDiagnostic("Preflight complete", { ready: preflight.ready, blockedDatasets: preflight.blocked_datasets, elapsedMs: elapsedSince(preflightStartedAt) });
      if (!preflight.ready && preflight.issues.some((issue) => ["missing_dataset", "insufficient_historical_depth", "feature_generation_failure", "stale_data"].includes(issue.classification))) {
        setPhase("preparing");
        const prepareStartedAt = now();
        logLaunchDiagnostic("Prepare started", { assets: nextSelection.assets.length, timeframes: selectedTimeframes });
        const prepared = await prepareResearchCampaign(
          nextSelection.assets.map((asset) => asset.apiSymbol),
          selectedTimeframes
        );
        if (!aliveRef.current || generation !== runGenerationRef.current) return;
        preflight = prepared.readiness;
        logLaunchDiagnostic("Prepare complete", { ready: preflight.ready, prepared: prepared.prepared.length, errors: prepared.errors.length, elapsedMs: elapsedSince(prepareStartedAt) });
      }
      const executableSymbols = new Set((preflight.executable_assets ?? []).map((symbol) => symbol.toUpperCase()));
      const campaignAssets = preflight.ready || !executableSymbols.size
        ? nextSelection.assets
        : nextSelection.assets.filter((asset) => executableSymbols.has(asset.apiSymbol.toUpperCase()));
      if (!preflight.ready && executableSymbols.size && campaignAssets.length < nextSelection.assets.length) {
        const missing = nextSelection.assets.length - campaignAssets.length;
        setLaunchError(`Only ${campaignAssets.length.toLocaleString()} of ${nextSelection.assets.length.toLocaleString()} selected assets are executable after data preparation. ${missing.toLocaleString()} selected assets still need candles or features, so no smaller campaign was launched. ${formatPreflightBlockers(preflight)}`);
        setPhase("error");
        return;
      }
      const campaignSelection = campaignAssets.length === nextSelection.assets.length
        ? nextSelection
        : buildResearchSelection("custom", campaignAssets, {
            universeMode: nextSelection.universeMode,
            evidenceAllocationPct: nextSelection.evidenceAllocationPct,
            guidanceSnapshotKey: nextSelection.guidanceSnapshotKey,
            establishedStrategyFamilies: nextSelection.establishedStrategyFamilies,
            timeframes: selectedTimeframes
          });

      if (!preflight.ready && !campaignAssets.length) {
        const firstIssue = preflight.issues[0];
        const issueDetail = firstIssue
          ? `${firstIssue.symbol} ${firstIssue.timeframe}: ${firstIssue.reason}`
          : "Required candle or feature data is unavailable.";
        setLaunchError(`Market data is not ready for this campaign. ${preflight.blocked_datasets.toLocaleString()} of ${preflight.datasets_total.toLocaleString()} datasets are unavailable. ${issueDetail}`);
        setPhase("error");
        return;
      }

      if (campaignSelection !== nextSelection) {
        setSelection(campaignSelection);
      }

      const universeKey = researchUniverseKey(campaignSelection);
      const universeStartedAt = now();
      logLaunchDiagnostic("Universe save started", { universeKey, assets: campaignAssets.length, excludedAssets: preflight.excluded_assets_total ?? 0 });
      await saveResearchUniverse({
        universe_key: universeKey,
        name: `${campaignSelection.scopeLabel} research universe`,
        description: "User-defined research universe created from the KefTrade Home research builder.",
        assets: campaignSelection.assets.map((asset) => asset.apiSymbol),
        default_timeframes: selectedTimeframes,
        metadata: {
          source: "home_research_builder",
          scope: nextSelection.scopeId,
          universe_mode: nextSelection.universeMode ?? "random",
          evidence_allocation_pct: nextSelection.evidenceAllocationPct ?? null,
          guidance_snapshot_key: nextSelection.guidanceSnapshotKey ?? null,
          established_strategy_families: nextSelection.establishedStrategyFamilies ?? [],
          established_timeframes: nextSelection.timeframes ?? [],
          executable_scope: campaignSelection.scopeId,
          display_assets: campaignSelection.assets.map((asset) => asset.id),
          requested_assets: nextSelection.assets.map((asset) => asset.apiSymbol),
          excluded_assets: preflight.excluded_assets ?? []
        }
      });
      logLaunchDiagnostic("Universe save complete", { universeKey, elapsedMs: elapsedSince(universeStartedAt) });

      const createStartedAt = now();
      logLaunchDiagnostic("Campaign create request sent", { universeKey, maxCandidates: campaignSelection.candidateCount, assetLimit: campaignSelection.assets.length });
      const portfolioEvidence = campaignSelection.scopeId === "portfolio";
      const created = await createResearchCampaign({
        universeKey,
        name: `${campaignSelection.scopeLabel} hypothesis research`,
        maxCandidates: campaignSelection.candidateCount,
        assetLimit: campaignSelection.assets.length,
        timeframes: selectedTimeframes,
        architectureMode: "legacy",
        datasetMode: portfolioEvidence ? "reproducibility" : "rolling",
        searchMode: "scout_expand"
      });
      if (!aliveRef.current || generation !== runGenerationRef.current) return;
      logLaunchDiagnostic("Campaign create response received", { campaignId: created.campaign.id, jobsCreated: created.jobs_created, elapsedMs: elapsedSince(createStartedAt) });

      setCreatedCampaign(created);
      const initialStatus = await getResearchCampaign(created.campaign.id);
      if (!aliveRef.current || generation !== runGenerationRef.current) return;
      setStatus(initialStatus);

      if (initialStatus.campaign.status === "completed") {
        setPhase("complete");
        return;
      }

      setPhase("running");
      void processCampaign(created.campaign.id, generation);
      logLaunchDiagnostic("Campaign launch workflow dispatched", { campaignId: created.campaign.id, elapsedMs: elapsedSince(launchStartedAt) });
    } catch (error) {
      if (!aliveRef.current || generation !== runGenerationRef.current) return;
      logLaunchException("Launch unexpected exception", error, { campaignId: createdCampaign?.campaign.id, elapsedMs: elapsedSince(launchStartedAt) });
      setLaunchError(readError(error));
      setPhase("error");
    }
  }

  function retryCampaign() {
    setLaunchError(null);
    const campaignId = createdCampaign?.campaign.id;
    if (campaignId) {
      const generation = runGenerationRef.current + 1;
      runGenerationRef.current = generation;
      setPhase("running");
      void processCampaign(campaignId, generation);
      return;
    }
    if (selection) void launchCampaign(selection);
  }

  function resetCampaign() {
    runGenerationRef.current += 1;
    setPhase("idle");
    setSelection(null);
    setCreatedCampaign(null);
    setStatus(null);
    setLaunchError(null);
    window.setTimeout(() => scrollToBuilder(reduceMotion), 30);
  }

  const reveal = reduceMotion ? undefined : {
    hidden: { opacity: 0, y: 18 },
    visible: { opacity: 1, y: 0 }
  };

  return (
    <motion.div
      className="pageContainer researchHome"
      initial="hidden"
      animate="visible"
      transition={{ staggerChildren: reduceMotion ? 0 : 0.08 }}
    >
      <motion.section className="researchHero" variants={reveal}>
        <div className="researchHeroCopy">
          <motion.span className="eyebrow" variants={reveal}>KefTrade research engine</motion.span>
          <motion.h1 variants={reveal}>Research the Market</motion.h1>
          <motion.p variants={reveal}>Turn measured market behavior into versioned hypotheses, focused strategy campaigns, and reproducible research evidence.</motion.p>
          <motion.div className="researchHeroActions" variants={reveal}>
            <motion.button
              type="button"
              className="button heroResearchButton"
              onClick={() => scrollToBuilder(reduceMotion)}
              whileHover={reduceMotion ? undefined : { y: -3 }}
              whileTap={reduceMotion ? undefined : { scale: 0.985 }}
            >
              <Sparkles size={19} /> Start Research Campaign <ArrowRight size={19} />
            </motion.button>
            <Link className="heroEvidenceLink" href="/research-intelligence">Browse research evidence <ArrowRight size={15} /></Link>
          </motion.div>
        </div>

        <motion.div
          className="researchHeroVisual"
          variants={reveal}
          aria-hidden="true"
          animate={reduceMotion ? undefined : { y: [0, -6, 0] }}
          transition={{ duration: 7, repeat: Infinity, ease: "easeInOut" }}
        >
          <div className="researchFieldLines">{Array.from({ length: 6 }, (_, index) => <span key={index} />)}</div>
          <div className="researchMarkFrame">
            <span className="markCoordinate top">GENERATE / VALIDATE</span>
            <Image src="/kefcore-mark.png" alt="" width={340} height={340} priority />
            <span className="markCoordinate bottom">REJECT / RANK / PROMOTE</span>
          </div>
        </motion.div>

        <motion.button type="button" className="heroScrollCue" onClick={() => scrollToBuilder(reduceMotion)} variants={reveal} aria-label="Go to research builder">
          <ArrowDown size={15} /> Build a campaign
        </motion.button>
      </motion.section>

      <motion.section className="researchJourney" variants={reveal} aria-label="How KefTrade researches">
        {journey.map((item) => (
          <article key={item.number}>
            <span>{item.number}</span>
            <div><strong>{item.title}</strong><p>{item.detail}</p></div>
            <Check size={16} />
          </article>
        ))}
      </motion.section>

      <CampaignActivity enabled />

      <StrategyLibraryPanel />

      {serviceError ? (
        <motion.div className="researchServiceNote" variants={reveal}>
          <ShieldCheck size={16} /><span>The workspace is available, but research services are not responding yet. Campaign launch will retry the connection.</span>
        </motion.div>
      ) : null}

      <AnimatePresence mode="wait">
        {phase === "idle" ? (
          <ResearchBuilder key="builder" launching={false} onLaunch={(nextSelection) => void launchCampaign(nextSelection)} />
        ) : selection ? (
          <ResearchLaunchExperience
            key="launch"
            phase={phase}
            selection={selection}
            createdCampaign={createdCampaign}
            status={status}
            error={launchError}
            onRetry={retryCampaign}
            onReset={resetCampaign}
          />
        ) : null}
      </AnimatePresence>

      <motion.footer className="researchHomeFooter" variants={reveal}>
        <span><Search size={15} /> Versioned hypothesis research</span>
        <span><ShieldCheck size={15} /> Simulation only</span>
        <Link href="/research">Research archive <ArrowRight size={14} /></Link>
      </motion.footer>
    </motion.div>
  );
}

function scrollToBuilder(reduceMotion: boolean | null) {
  document.getElementById("research-builder")?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
}

function readError(error: unknown) {
  if (error instanceof Error) {
    if (error.name === "AbortError") return "The research service took too long to respond. The campaign evidence remains preserved.";
    return `The research service returned ${error.message}.`;
  }
  return "KefTrade could not continue the campaign. Completed evidence remains preserved.";
}

function now() {
  return typeof performance === "undefined" ? Date.now() : performance.now();
}

function elapsedSince(startedAt: number) {
  return Math.round(now() - startedAt);
}

function formatPreflightBlockers(preflight: ResearchCampaignPreflight) {
  const classSummary = Object.entries(preflight.classifications ?? {})
    .map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`)
    .join("; ");
  const issuesBySymbol = new Map<string, string[]>();
  for (const issue of preflight.issues ?? []) {
    const parts = [`${issue.timeframe} ${issue.classification.replaceAll("_", " ")}`];
    if (issue.reason) parts.push(issue.reason);
    const existing = issuesBySymbol.get(issue.symbol) ?? [];
    existing.push(parts.join(": "));
    issuesBySymbol.set(issue.symbol, existing);
  }
  const excludedDetails = (preflight.excluded_assets ?? [])
    .slice(0, 8)
    .map((symbol) => {
      const details = issuesBySymbol.get(symbol);
      return details?.length ? `${symbol} (${details.join("; ")})` : symbol;
    })
    .join(", ");
  const remaining = Math.max(0, (preflight.excluded_assets_total ?? 0) - 8);
  const excludedSummary = excludedDetails
    ? `Excluded: ${excludedDetails}${remaining ? `, +${remaining.toLocaleString()} more` : ""}.`
    : "Excluded symbols were not reported.";
  return `${excludedSummary} ${classSummary ? `Issue totals: ${classSummary}.` : ""}`;
}

function logLaunchDiagnostic(message: string, fields: Record<string, unknown>) {
  if (process.env.NODE_ENV === "production" && process.env.NEXT_PUBLIC_DIAGNOSTIC_LOGGING !== "true") return;
  console.info(`[research-launch] ${message}`, fields);
}

function logLaunchException(message: string, error: unknown, fields: Record<string, unknown>) {
  if (process.env.NODE_ENV === "production" && process.env.NEXT_PUBLIC_DIAGNOSTIC_LOGGING !== "true") return;
  console.error(`[research-launch] ${message}`, {
    ...fields,
    exceptionClass: error instanceof Error ? error.name : typeof error,
    exceptionMessage: error instanceof Error ? error.message : String(error),
    traceback: error instanceof Error ? error.stack : undefined
  });
}
