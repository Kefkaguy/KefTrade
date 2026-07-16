"use client";

import Image from "next/image";
import Link from "next/link";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowRight, Check, ExternalLink, FlaskConical, RotateCcw, Sparkles, Trophy } from "lucide-react";
import type { ResearchCampaignCreateResult, ResearchCampaignStatus } from "@/lib/api";
import type { ResearchSelection } from "@/lib/home-research";

export type ResearchLaunchPhase = "creating" | "running" | "complete" | "error";

type ResearchLaunchExperienceProps = {
  phase: ResearchLaunchPhase;
  selection: ResearchSelection;
  createdCampaign: ResearchCampaignCreateResult | null;
  status: ResearchCampaignStatus | null;
  error: string | null;
  onRetry: () => void;
  onReset: () => void;
};

const researchStages = [
  { title: "Finding market opportunities", detail: "Scanning selected assets across connected market data." },
  { title: "Testing strategy ideas", detail: "Running deterministic strategy variations through the research engine." },
  { title: "Rejecting weak strategies", detail: "Removing ideas that fail evidence and consistency requirements." },
  { title: "Promoting strong evidence", detail: "Advancing candidates that remain stable through validation." },
  { title: "Searching for elite strategies", detail: "Ranking the strongest surviving research candidates." }
] as const;

export function ResearchLaunchExperience({
  phase,
  selection,
  createdCampaign,
  status,
  error,
  onRetry,
  onReset
}: ResearchLaunchExperienceProps) {
  const reduceMotion = useReducedMotion();
  const overallProgress = campaignProgress(phase, status);
  const eliteCandidates = status?.elite_candidates ?? [];
  const jobsByStatus = status?.analytics.jobs_by_status ?? {};
  const processed = terminalJobs(jobsByStatus);
  const total = Number(status?.analytics.jobs_total ?? createdCampaign?.jobs_created ?? selection.estimatedJobs);

  return (
    <motion.section
      className="researchLaunchExperience"
      aria-live="polite"
      initial={reduceMotion ? false : { opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0 }}
      exit={reduceMotion ? undefined : { opacity: 0, y: -16 }}
      transition={{ duration: 0.48, ease: [0.22, 1, 0.36, 1] }}
    >
      <div className="launchHeader">
        <div className="launchSignal" aria-hidden="true">
          <motion.span
            animate={reduceMotion || phase === "complete" ? undefined : { x: ["-70%", "70%", "-70%"] }}
            transition={{ duration: 4.5, repeat: Infinity, ease: "easeInOut" }}
          />
          <Image src="/kefcore-mark.png" alt="" width={118} height={118} />
        </div>
        <div>
          <span className="eyebrow">{phaseLabel(phase, Boolean(createdCampaign))}</span>
          <h2>{phaseTitle(phase, selection.scopeLabel, Boolean(createdCampaign))}</h2>
          <p>{phaseDescription(phase, selection.assets.length, Boolean(createdCampaign))}</p>
        </div>
        {phase !== "creating" ? (
          <div className="launchProgressValue"><strong>{Math.round(overallProgress)}%</strong><span>{processed.toLocaleString()} of {total.toLocaleString()} jobs</span></div>
        ) : null}
      </div>

      <AnimatePresence mode="wait">
        {phase === "error" ? (
          <motion.div key="error" className="launchError" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <FlaskConical size={22} />
            <div><strong>The campaign needs attention</strong><p>{error ?? "KefTrade could not continue this research batch."}</p></div>
            <div className="launchErrorActions">
              <button type="button" className="button" onClick={onRetry}><RotateCcw size={16} /> {createdCampaign ? "Retry" : "Check again"}</button>
              <button type="button" className="button secondary" onClick={onReset}>Edit scope</button>
            </div>
          </motion.div>
        ) : (
          <motion.div key="timeline" className="researchTimeline" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            {researchStages.map((stage, index) => {
              const progress = stageProgress(index, overallProgress, phase);
              const complete = progress >= 100;
              const active = progress > 0 && progress < 100;
              return (
                <motion.article
                  key={stage.title}
                  className={`${complete ? "complete" : ""} ${active ? "active" : ""}`}
                  initial={reduceMotion ? false : { opacity: 0, x: -12 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: reduceMotion ? 0 : index * 0.07 }}
                >
                  <span className="timelineIndex">{complete ? <Check size={15} /> : String(index + 1).padStart(2, "0")}</span>
                  <div className="timelineCopy"><strong>{stage.title}</strong><small>{stage.detail}</small></div>
                  <div className="timelineProgress" aria-label={`${stage.title}: ${Math.round(progress)} percent complete`}>
                    <motion.i animate={{ width: `${progress}%` }} transition={{ duration: reduceMotion ? 0 : 0.6, ease: [0.22, 1, 0.36, 1] }} />
                  </div>
                  <span className="timelinePercent">{Math.round(progress)}%</span>
                </motion.article>
              );
            })}
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {eliteCandidates.length > 0 ? (
          <motion.div
            className="eliteDiscovery"
            layoutId="elite-discovery"
            initial={reduceMotion ? false : { opacity: 0, y: 18, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0 }}
          >
            <span className="eliteIcon"><Trophy size={21} /></span>
            <div><span className="eyebrow">Elite discovery</span><strong>{eliteCandidates.length} evidence-backed {eliteCandidates.length === 1 ? "strategy" : "strategies"} found</strong><p>The strongest candidates are ready for deeper review and forward validation.</p></div>
            <Link className="button secondary" href="/research-intelligence">Review evidence <ExternalLink size={15} /></Link>
          </motion.div>
        ) : null}
      </AnimatePresence>

      {phase === "complete" ? (
        <motion.div className="launchCompleteActions" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
          <div><Sparkles size={18} /><span>Campaign complete. The full evidence record has been preserved.</span></div>
          <div><Link className="button" href="/research">Open campaign <ArrowRight size={16} /></Link><button type="button" className="button secondary" onClick={onReset}>Start another</button></div>
        </motion.div>
      ) : null}
    </motion.section>
  );
}

function campaignProgress(phase: ResearchLaunchPhase, status: ResearchCampaignStatus | null) {
  if (phase === "complete") return 100;
  if (phase === "creating") return 0;
  const progress = Number(status?.analytics.completion_percentage ?? 0);
  return Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : 0;
}

function stageProgress(index: number, overall: number, phase: ResearchLaunchPhase) {
  if (phase === "complete") return 100;
  if (phase === "creating") return index === 0 ? 18 : 0;
  const starts = [0, 0, 16, 38, 72];
  const spans = [1, 42, 38, 34, 28];
  if (index === 0) return 100;
  return Math.max(0, Math.min(100, ((overall - starts[index]) / spans[index]) * 100));
}

function terminalJobs(statuses: Record<string, number>) {
  return ["completed", "promoted", "rejected", "failed", "canceled"]
    .reduce((sum, key) => sum + Number(statuses[key] ?? 0), 0);
}

function phaseLabel(phase: ResearchLaunchPhase, campaignCreated: boolean) {
  if (phase === "creating") return "Preparing deterministic campaign";
  if (phase === "running") return "Research in progress";
  if (phase === "complete") return "Research complete";
  return campaignCreated ? "Research interrupted" : "Research preflight blocked";
}

function phaseTitle(phase: ResearchLaunchPhase, scope: string, campaignCreated: boolean) {
  if (phase === "creating") return "Building your research environment";
  if (phase === "complete") return `${scope} search complete`;
  if (phase === "error") return campaignCreated ? "Research paused safely" : "Market data is not ready";
  return `Searching ${scope}`;
}

function phaseDescription(phase: ResearchLaunchPhase, assetCount: number, campaignCreated: boolean) {
  if (phase === "creating") return "Backfilling market history, calculating research features, and preparing deterministic validation jobs.";
  if (phase === "complete") return "Every candidate has been tested, classified, and preserved with its supporting evidence.";
  if (phase === "error") return campaignCreated
    ? "Completed evidence remains preserved. Retry the current batch or return to the builder."
    : "No campaign was created. KefTrade attempted to prepare the required market history and preserved the exact dataset issue for review.";
  return `KefTrade is testing strategy ideas across ${assetCount} ${assetCount === 1 ? "asset" : "assets"} and rejecting weak evidence as it works.`;
}
