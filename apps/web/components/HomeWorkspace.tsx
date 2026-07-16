"use client";

import Image from "next/image";
import Link from "next/link";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowDown, ArrowRight, Check, Search, ShieldCheck, Sparkles } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { CampaignActivity } from "@/components/CampaignActivity";
import { ResearchBuilder } from "@/components/ResearchBuilder";
import { ResearchLaunchExperience, type ResearchLaunchPhase } from "@/components/ResearchLaunchExperience";
import {
  createResearchCampaign,
  getResearchCampaign,
  prepareResearchCampaign,
  preflightResearchCampaign,
  runResearchCampaignBatch,
  saveResearchUniverse,
  type MissionControlSnapshot,
  type ResearchCampaignCreateResult,
  type ResearchCampaignStatus
} from "@/lib/api";
import { RESEARCH_TIMEFRAMES, researchUniverseKey, type ResearchSelection } from "@/lib/home-research";

type HomeWorkspaceProps = {
  snapshot: MissionControlSnapshot | null;
  error?: string | null;
};

const journey = [
  { number: "01", title: "Choose the market", detail: "Research one asset, a focused sector, or every connected market." },
  { number: "02", title: "Search systematically", detail: "Generate and test deterministic strategy variations across market conditions." },
  { number: "03", title: "Promote the evidence", detail: "Reject weak ideas and advance only repeatable, evidence-backed candidates." }
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
    try {
      while (aliveRef.current && generation === runGenerationRef.current) {
        const batch = await runResearchCampaignBatch(campaignId, 50);
        if (!aliveRef.current || generation !== runGenerationRef.current) return;

        const nextStatus = await getResearchCampaign(campaignId);
        if (!aliveRef.current || generation !== runGenerationRef.current) return;
        setStatus(nextStatus);

        const campaignState = nextStatus.campaign.status.toLowerCase();
        const blockedJobs = Number(nextStatus.analytics.jobs_by_status?.blocked_data ?? 0);
        if (["paused", "failed", "canceled"].includes(campaignState)) {
          setLaunchError(`Campaign ${campaignId} is ${campaignState}. Completed and blocked job records remain preserved.`);
          setPhase("error");
          return;
        }
        if (blockedJobs > 0) {
          setLaunchError(`${blockedJobs.toLocaleString()} jobs were blocked because required market data or features are unavailable. The remaining queue has been stopped.`);
          setPhase("error");
          return;
        }

        const campaignComplete = nextStatus.campaign.status === "completed" || batch.remaining === 0;
        if (campaignComplete) {
          setPhase("complete");
          return;
        }

        if (batch.processed === 0) {
          setLaunchError("No campaign jobs were eligible to run. Check market-data coverage before retrying.");
          setPhase("error");
          return;
        }
        await delay(700);
      }
    } catch (error) {
      if (!aliveRef.current || generation !== runGenerationRef.current) return;
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
      try {
        const nextStatus = await getResearchCampaign(campaignId);
        if (!aliveRef.current) return;
        setStatus(nextStatus);
        if (nextStatus.campaign.status === "completed") setPhase("complete");
        if (["paused", "failed", "canceled"].includes(nextStatus.campaign.status.toLowerCase())) {
          setLaunchError(`Campaign ${campaignId} is ${nextStatus.campaign.status.toLowerCase()}. No additional jobs will be started.`);
          setPhase("error");
        }
      } catch {
        // The active execution request can briefly hold the local API; the next poll retries.
      } finally {
        pollingRef.current = false;
      }
    }, 5000);

    return () => window.clearInterval(timer);
  }, [createdCampaign?.campaign.id, phase]);

  async function launchCampaign(nextSelection: ResearchSelection) {
    const generation = runGenerationRef.current + 1;
    runGenerationRef.current = generation;
    setSelection(nextSelection);
    setCreatedCampaign(null);
    setStatus(null);
    setLaunchError(null);
    setPhase("creating");

    try {
      let preflight = await preflightResearchCampaign(
        nextSelection.assets.map((asset) => asset.apiSymbol),
        [...RESEARCH_TIMEFRAMES]
      );
      if (!aliveRef.current || generation !== runGenerationRef.current) return;
      if (!preflight.ready) {
        const preparation = await prepareResearchCampaign(
          nextSelection.assets.map((asset) => asset.apiSymbol),
          [...RESEARCH_TIMEFRAMES]
        );
        if (!aliveRef.current || generation !== runGenerationRef.current) return;
        preflight = preparation.readiness;
      }
      if (!preflight.ready) {
        const firstIssue = preflight.issues[0];
        const issueDetail = firstIssue
          ? `${firstIssue.symbol} ${firstIssue.timeframe}: ${firstIssue.reason}`
          : "Required candle or feature data is unavailable.";
        setLaunchError(`Market data preparation could not complete. ${preflight.blocked_datasets.toLocaleString()} of ${preflight.datasets_total.toLocaleString()} datasets are still unavailable. ${issueDetail}`);
        setPhase("error");
        return;
      }

      const universeKey = researchUniverseKey(nextSelection);
      await saveResearchUniverse({
        universe_key: universeKey,
        name: `${nextSelection.scopeLabel} research universe`,
        description: "User-defined research universe created from the KefTrade Home research builder.",
        assets: nextSelection.assets.map((asset) => asset.apiSymbol),
        default_timeframes: [...RESEARCH_TIMEFRAMES],
        metadata: {
          source: "home_research_builder",
          scope: nextSelection.scopeId,
          display_assets: nextSelection.assets.map((asset) => asset.id)
        }
      });

      const created = await createResearchCampaign({
        universeKey,
        name: `${nextSelection.scopeLabel} deterministic discovery`,
        maxCandidates: nextSelection.candidateCount,
        assetLimit: nextSelection.assets.length,
        timeframes: [...RESEARCH_TIMEFRAMES]
      });
      if (!aliveRef.current || generation !== runGenerationRef.current) return;

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
    } catch (error) {
      if (!aliveRef.current || generation !== runGenerationRef.current) return;
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
          <motion.p variants={reveal}>Discover evidence-based quantitative strategies across thousands of market conditions. Build deterministic research campaigns powered by KefTrade.</motion.p>
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
        <span><Search size={15} /> Deterministic strategy discovery</span>
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

function delay(milliseconds: number) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}
