"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Cpu, Gauge, MemoryStick, Pause, Play, RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  controlResearchCampaign,
  deleteResearchCampaign,
  getResearchCampaignProfile,
  getResearchCampaigns,
  runParallelResearchCampaign,
  type ResearchCampaignList,
  type ResearchCampaignListRow,
  type ResearchCampaignProfile
} from "@/lib/api";

type CampaignFilter = "all" | "running" | "paused";

export function CampaignActivity({ enabled = true }: { enabled?: boolean }) {
  const reduceMotion = useReducedMotion();
  const [data, setData] = useState<ResearchCampaignList | null>(null);
  const [filter, setFilter] = useState<CampaignFilter>("all");
  const [busyId, setBusyId] = useState<number | null>(null);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [executionId, setExecutionId] = useState<number | null>(null);
  const [workerCount, setWorkerCount] = useState(1);
  const [profile, setProfile] = useState<ResearchCampaignProfile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const listRequestActive = useRef(false);
  const profileRequestActive = useRef(false);
  const profileRequestSucceeded = useRef(false);

  const refresh = useCallback(async (showActivity = false) => {
    if (!enabled || listRequestActive.current) return;
    listRequestActive.current = true;
    if (showActivity) setRefreshing(true);
    try {
      const next = await getResearchCampaigns();
      setData(next);
      setError(null);
    } catch (requestError) {
      setError(readCampaignLoadError(requestError));
    } finally {
      listRequestActive.current = false;
      if (showActivity) setRefreshing(false);
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!executionId) return;
    const refreshProfile = async () => {
      if (profileRequestActive.current) return;
      profileRequestActive.current = true;
      try {
        const nextProfile = await getResearchCampaignProfile(executionId);
        profileRequestSucceeded.current = true;
        setProfile(nextProfile);
        setError(null);
        if (nextProfile.runtime.parallel_pool_active) {
          setWorkerCount(nextProfile.runtime.configured_parallel_workers || 1);
        }
      } catch (requestError) {
        if (!profileRequestSucceeded.current) setError(readCampaignError(requestError));
      } finally {
        profileRequestActive.current = false;
      }
    };
    void refreshProfile();
    const timer = window.setInterval(() => void refreshProfile(), 5000);
    return () => window.clearInterval(timer);
  }, [executionId]);

  const campaigns = useMemo(() => (data?.campaigns ?? []).filter((campaign) => {
    if (filter === "running") return campaign.status === "running";
    if (filter === "paused") return campaign.status === "paused";
    return true;
  }), [data?.campaigns, filter]);

  async function changeState(campaign: ResearchCampaignListRow) {
    const action = campaign.status === "paused" ? "resume" : "pause";
    setBusyId(campaign.id);
    setError(null);
    try {
      await controlResearchCampaign(campaign.id, action);
      await refresh();
    } catch (requestError) {
      setError(readCampaignError(requestError));
    } finally {
      setBusyId(null);
    }
  }

  async function removeCampaign(campaignId: number) {
    setBusyId(campaignId);
    setError(null);
    try {
      await deleteResearchCampaign(campaignId, true);
      setDeleteId(null);
      if (executionId === campaignId) {
        setExecutionId(null);
        setProfile(null);
      }
      await refresh();
    } catch (requestError) {
      setError(readCampaignError(requestError));
    } finally {
      setBusyId(null);
    }
  }

  async function openExecution(campaignId: number) {
    const nextId = executionId === campaignId ? null : campaignId;
    setExecutionId(nextId);
    setProfile(null);
    if (!nextId) return;
    profileRequestSucceeded.current = false;
    setWorkerCount(1);
  }

  async function runParallel(campaignId: number) {
    setBusyId(campaignId);
    setError(null);
    try {
      await runParallelResearchCampaign(campaignId, workerCount);
      setProfile(await getResearchCampaignProfile(campaignId));
      await refresh();
    } catch (requestError) {
      setError(readCampaignError(requestError));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <motion.section
      className="campaignActivity"
      initial={reduceMotion ? false : { opacity: 0, y: 18 }}
      whileInView={reduceMotion ? undefined : { opacity: 1, y: 0 }}
      viewport={{ once: true, amount: 0.15 }}
      aria-labelledby="campaign-activity-title"
    >
      <header className="campaignActivityHeader">
        <div>
          <span className="eyebrow">Research operations</span>
          <h2 id="campaign-activity-title">Campaign activity</h2>
          <p>See what is running, pause work, or resume it when the market data is ready.</p>
        </div>
        <div className="campaignActivitySummary" aria-label="Campaign status summary">
          <span><i className="campaignStatusDot running" /> <strong>{data ? data.summary.running : "—"}</strong> Running</span>
          <span><i className="campaignStatusDot queued" /> <strong>{data ? data.summary.queued : "—"}</strong> Queued</span>
          <span><i className="campaignStatusDot paused" /> <strong>{data ? data.summary.paused : "—"}</strong> Paused</span>
          <button className="campaignIconButton" type="button" onClick={() => void refresh(true)} disabled={refreshing || !enabled} title="Refresh campaigns" aria-label="Refresh campaigns">
            <RefreshCw size={16} className={refreshing ? "isSpinning" : undefined} />
          </button>
        </div>
      </header>

      <div className="campaignFilters" role="group" aria-label="Filter campaigns">
        {(["all", "running", "paused"] as const).map((option) => (
          <button key={option} type="button" className={filter === option ? "active" : undefined} onClick={() => setFilter(option)}>
            {option === "all" ? "All" : option === "running" ? "Running" : "Paused"}
          </button>
        ))}
      </div>

      {error ? <div className="campaignActivityError" role="alert">{error}</div> : null}

      <div className="campaignRows">
        {!data && enabled ? <CampaignLoading /> : null}
        {!enabled ? <div className="campaignActivityEmpty">Research services are unavailable.</div> : null}
        {data && campaigns.length === 0 ? <div className="campaignActivityEmpty">No {filter === "all" ? "saved" : filter} campaigns.</div> : null}
        <AnimatePresence initial={false}>
          {campaigns.map((campaign) => {
            const progress = campaign.total_jobs > 0 ? Math.min(100, Math.round((campaign.terminal_jobs / campaign.total_jobs) * 100)) : 0;
            const isPaused = campaign.status === "paused";
            const canControl = ["running", "queued", "paused", "failed"].includes(campaign.status);
            const canDelete = !["running", "completed"].includes(campaign.status);
            const isActive = ["running", "queued"].includes(campaign.status);
            const eta = isActive ? formatEta(campaign.eta_seconds ?? campaign.estimated_seconds_remaining, campaign.eta_method) : null;
            const poolActive = Boolean(profile?.runtime.parallel_pool_active) && executionId === campaign.id;
            const poolStarting = poolActive && ((profile?.runtime.starting_parallel_workers ?? 0) > 0 || (profile?.runtime.live_workers ?? 0) < (profile?.runtime.target_workers ?? 0));
            const startingWorkers = Math.max(profile?.runtime.starting_parallel_workers ?? 0, (profile?.runtime.target_workers ?? 0) - (profile?.runtime.live_workers ?? 0));
            const workerLimit = profile?.runtime.worker_limit ?? 8;
            const workerOptions = [1, 2, 4, 8].filter((count) => count <= workerLimit);
            return (
              <motion.article key={campaign.id} className="campaignRow" layout initial={reduceMotion ? false : { opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, height: 0 }}>
                <div className="campaignState">
                  <i className={`campaignStatusDot ${campaign.status}`} />
                  <span>{campaign.status.replaceAll("_", " ")}</span>
                </div>
                <div className="campaignIdentity">
                  <strong>{campaign.name}</strong>
                  <small>Campaign {campaign.id} · {campaign.dataset_id ? "versioned research" : "legacy research"} · {campaign.universe_key}</small>
                  {campaign.search_mode === "scout_expand" ? <small>{researchStageLabel(campaign)}</small> : null}
                </div>
                <div className="campaignProgress">
                  <div><span>{campaign.terminal_jobs.toLocaleString()} of {campaign.total_jobs.toLocaleString()} jobs</span><strong>{progress}%</strong></div>
                  <span className="campaignProgressTrack"><motion.i initial={false} animate={{ width: `${progress}%` }} transition={{ duration: reduceMotion ? 0 : 0.45 }} /></span>
                  <small>{campaign.queued_jobs.toLocaleString()} queued · {campaign.blocked_jobs.toLocaleString()} blocked</small>
                </div>
                <div className="campaignTiming">
                  {eta ? <strong title={eta.title}>{eta.label}</strong> : null}
                  <time dateTime={campaign.updated_at}>{relativeTime(campaign.updated_at)}</time>
                </div>
                <div className="campaignRowActions">
                  <button className="campaignIconButton" type="button" onClick={() => void openExecution(campaign.id)} disabled={!['queued', 'running'].includes(campaign.status) || busyId === campaign.id} title={['queued', 'running'].includes(campaign.status) ? "Parallel execution and profiling" : "Resume before running simulations"} aria-label={`Configure parallel execution for ${campaign.name}`}>
                    <Gauge size={16} />
                  </button>
                  {canControl ? (
                    <button className="campaignIconButton" type="button" onClick={() => void changeState(campaign)} disabled={busyId === campaign.id} title={isPaused ? "Resume campaign" : "Pause campaign"} aria-label={isPaused ? `Resume ${campaign.name}` : `Pause ${campaign.name}`}>
                      {isPaused ? <Play size={16} /> : <Pause size={16} />}
                    </button>
                  ) : null}
                  <button className="campaignIconButton danger" type="button" onClick={() => setDeleteId(campaign.id)} disabled={!canDelete || busyId === campaign.id} title={canDelete ? "Delete campaign" : campaign.status === "running" ? "Pause before deleting" : "Completed evidence cannot be deleted"} aria-label={`Delete ${campaign.name}`}>
                    <Trash2 size={16} />
                  </button>
                </div>
                <AnimatePresence>
                  {executionId === campaign.id ? (
                    <motion.div className="campaignExecutionPanel" initial={reduceMotion ? false : { opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
                      <div className="campaignExecutionHeading"><div><strong>Parallel simulations</strong><span>Keep the selected worker pool active until the campaign is paused or finished.</span></div><small>Optional</small></div>
                      <div className="workerSelector" role="group" aria-label="Concurrent simulation workers">
                        {workerOptions.map((count) => <button key={count} type="button" className={workerCount === count ? "active" : undefined} onClick={() => setWorkerCount(count)} disabled={busyId === campaign.id}>{count} worker{count === 1 ? "" : "s"}</button>)}
                      </div>
                      {profile ? (
                        <div className="campaignRuntime" aria-label="Live parallel worker resource usage">
                          <span><Cpu size={15} /><small>Workers alive</small><strong>{profile.runtime.live_workers ?? profile.runtime.active_parallel_workers} / {profile.runtime.target_workers || profile.runtime.configured_parallel_workers || workerCount}</strong></span>
                          <span><Gauge size={15} /><small>Jobs claimed</small><strong>{profile.runtime.active_parallel_jobs}</strong></span>
                          <span title="Resident memory used by the KefTrade API process, including parallel worker threads"><MemoryStick size={15} /><small>API RAM</small><strong>{formatMemory(profile.runtime.resident_memory_mb)}</strong></span>
                          <i className={poolActive ? "active" : undefined}>{poolStarting ? `Starting ${startingWorkers} worker${startingWorkers === 1 ? "" : "s"}` : poolActive ? `${profile.runtime.effective_workers ?? profile.runtime.active_parallel_workers} effective / ${profile.runtime.draining_workers ?? 0} draining` : "Workers idle"}</i>
                        </div>
                      ) : null}
                      {profile?.profiled_jobs ? (
                        <div className="campaignProfile" aria-label="Average campaign pipeline timings">
                          <ProfileMetric label="Data" value={profile.average_ms.loading_market_data} />
                          <ProfileMetric label="Indicators" value={profile.average_ms.calculating_indicators} />
                          <ProfileMetric label="Simulation" value={profile.average_ms.running_simulation} />
                          <ProfileMetric label="Database" value={profile.average_ms.database_queue_operations + profile.average_ms.writing_results} />
                          <ProfileMetric label="Total" value={profile.average_ms.total} />
                        </div>
                      ) : <p className="campaignProfileEmpty">Timing data appears after the first completed batch.</p>}
                      <button type="button" className="button" onClick={() => void runParallel(campaign.id)} disabled={busyId === campaign.id}>{busyId === campaign.id || poolStarting ? "Updating workers..." : poolActive ? `Set ${workerCount} worker${workerCount === 1 ? "" : "s"}` : `Start ${workerCount} worker${workerCount === 1 ? "" : "s"}`}</button>
                    </motion.div>
                  ) : null}
                  {deleteId === campaign.id ? (
                    <motion.div className="campaignDeleteConfirm" initial={reduceMotion ? false : { opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
                      <span>Delete this campaign, its queued jobs, and {campaign.terminal_jobs.toLocaleString()} stored result records?</span>
                      <button type="button" onClick={() => setDeleteId(null)} disabled={busyId === campaign.id}>Cancel</button>
                      <button type="button" className="danger" onClick={() => void removeCampaign(campaign.id)} disabled={busyId === campaign.id}>Delete campaign</button>
                    </motion.div>
                  ) : null}
                </AnimatePresence>
              </motion.article>
            );
          })}
        </AnimatePresence>
      </div>
    </motion.section>
  );
}

function ProfileMetric({ label, value }: { label: string; value: number }) {
  return <span><small>{label}</small><strong>{value.toLocaleString(undefined, { maximumFractionDigits: 1 })} ms</strong></span>;
}

function formatMemory(valueMb: number) {
  if (valueMb >= 1024) return `${(valueMb / 1024).toLocaleString(undefined, { maximumFractionDigits: 2 })} GB`;
  return `${valueMb.toLocaleString(undefined, { maximumFractionDigits: 1 })} MB`;
}

function CampaignLoading() {
  return <div className="campaignActivityEmpty">Loading campaign activity...</div>;
}

function researchStageLabel(campaign: ResearchCampaignListRow) {
  if (campaign.research_stage === "scout") return `Scout pass · ${campaign.scout_candidate_count ?? 0} diverse candidates`;
  if (campaign.research_stage === "expanded") return `Focused expansion · ${campaign.expanded_routes ?? 0} evidence-backed routes`;
  if (campaign.research_stage === "stopped_no_signal") return "Scout stopped · no route met the expansion evidence floor";
  if (campaign.research_stage === "stopped_no_inventory") return "Scout complete · no unique expansion candidates remained";
  return "Full search";
}

function relativeTime(value: string) {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "Recently updated";
  const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
  if (minutes < 1) return "Updated now";
  if (minutes < 60) return `Updated ${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `Updated ${hours}h ago`;
  return `Updated ${Math.floor(hours / 24)}d ago`;
}

function formatEta(value: number | null | undefined, method?: string) {
  if (!value || value <= 0) {
    return { label: "Estimating...", title: method === "estimating" ? "Waiting for enough terminal jobs to calculate rolling throughput." : "Not enough recent completed jobs to estimate time remaining yet." };
  }
  const minutes = Math.ceil(value / 60);
  if (minutes < 60) {
    return { label: `ETA ${minutes}m`, title: `Estimated time left: ${minutes} minute${minutes === 1 ? "" : "s"}.` };
  }
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 24) {
    return {
      label: remainingMinutes ? `ETA ${hours}h ${remainingMinutes}m` : `ETA ${hours}h`,
      title: `Estimated time left: ${hours} hour${hours === 1 ? "" : "s"}${remainingMinutes ? ` ${remainingMinutes} minute${remainingMinutes === 1 ? "" : "s"}` : ""}.`
    };
  }
  const days = Math.floor(hours / 24);
  const remainingHours = hours % 24;
  return {
    label: remainingHours ? `ETA ${days}d ${remainingHours}h` : `ETA ${days}d`,
    title: `Estimated time left: ${days} day${days === 1 ? "" : "s"}${remainingHours ? ` ${remainingHours} hour${remainingHours === 1 ? "" : "s"}` : ""}.`
  };
}

function estimatedSecondsRemaining(campaign: ResearchCampaignListRow) {
  if (campaign.estimated_seconds_remaining && campaign.estimated_seconds_remaining > 0) {
    return campaign.estimated_seconds_remaining;
  }
  const remainingJobs = Math.max(campaign.total_jobs - campaign.terminal_jobs - campaign.blocked_jobs, 0);
  const startedAt = campaign.started_at ? new Date(campaign.started_at).getTime() : NaN;
  if (remainingJobs <= 0 || campaign.terminal_jobs <= 0 || !Number.isFinite(startedAt)) return null;
  const elapsedSeconds = Math.max((Date.now() - startedAt) / 1000, 1);
  const jobsPerSecond = campaign.terminal_jobs / elapsedSeconds;
  return jobsPerSecond > 0 ? Math.round(remainingJobs / jobsPerSecond) : null;
}

function readCampaignError(error: unknown) {
  return error instanceof Error ? `Campaign action failed: ${error.message}.` : "Campaign action failed. Try again.";
}

function readCampaignLoadError(error: unknown) {
  const detail = error instanceof Error && error.message ? ` (${error.message})` : "";
  return `Campaign activity is temporarily unavailable${detail}. Retrying automatically.`;
}
