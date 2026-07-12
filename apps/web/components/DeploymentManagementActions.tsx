"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import {
  bulkPauseDeployments,
  bulkScanDeployments,
  pauseStrategyDeployment,
  resumeStrategyDeployment,
  scanStrategyDeployment,
  updateDeploymentControls,
  type ManagedDeployment
} from "@/lib/api";
import { Toast } from "@/components/ResearchUI";

type ToastState = { tone: "success" | "error" | "info"; message: string };
type DeploymentCadence = "scheduler" | "manual" | "15m" | "30m" | "60m" | "daily";

export function BulkDeploymentControls({ activeIds }: { activeIds: number[] }) {
  const router = useRouter();
  const [busy, setBusy] = useState<"scan" | "pause" | null>(null);
  const [toast, setToast] = useState<ToastState>({ tone: "info", message: "" });

  async function run(action: "scan" | "pause") {
    setBusy(action);
    try {
      const result = action === "scan" ? await bulkScanDeployments(activeIds) : await bulkPauseDeployments(activeIds);
      const message = action === "scan"
        ? `Bulk scan completed: ${String(result.completed ?? 0)} completed / ${String(result.failed ?? 0)} failed.`
        : `Bulk pause completed: ${String(result.paused ?? 0)} deployment(s) paused.`;
      setToast({ tone: "success", message });
      router.refresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Bulk deployment action failed." });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="operationBar">
      <button className="button" type="button" disabled={busy !== null || activeIds.length === 0} onClick={() => run("scan")}>
        {busy === "scan" ? "Scanning..." : "Bulk scan active"}
      </button>
      <button className="button ghost danger" type="button" disabled={busy !== null || activeIds.length === 0} onClick={() => run("pause")}>
        {busy === "pause" ? "Pausing..." : "Bulk pause active"}
      </button>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}

export function DeploymentControlPanel({ deployment }: { deployment: ManagedDeployment }) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [cadence, setCadence] = useState<DeploymentCadence>((deployment.scan_cadence ?? "scheduler") as DeploymentCadence);
  const [limit, setLimit] = useState(Math.round(Number(deployment.max_simulated_exposure_pct ?? 0.1) * 100));
  const [toast, setToast] = useState<ToastState>({ tone: "info", message: "" });

  async function run(action: string, task: () => Promise<unknown>, success: string) {
    setBusy(action);
    try {
      await task();
      setToast({ tone: "success", message: success });
      router.refresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Deployment action failed." });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="deploymentControlPanel">
      <div className="operationBar compactControls">
        {deployment.status === "paused" ? (
          <button className="button compact" type="button" disabled={busy !== null} onClick={() => run("resume", () => resumeStrategyDeployment(deployment.id), "Deployment resumed.")}>
            {busy === "resume" ? "Resuming..." : "Resume"}
          </button>
        ) : (
          <button className="button compact ghost" type="button" disabled={busy !== null} onClick={() => run("pause", () => pauseStrategyDeployment(deployment.id), "Deployment paused.")}>
            {busy === "pause" ? "Pausing..." : "Pause"}
          </button>
        )}
        <button className="button compact secondary" type="button" disabled={busy !== null || deployment.status !== "active"} onClick={() => run("scan", () => scanStrategyDeployment(deployment.id), "Deployment scan finished.")}>
          {busy === "scan" ? "Scanning..." : "Scan"}
        </button>
      </div>
      <div className="formGrid deploymentControlsForm">
        <label className="field">
          <span>Cadence</span>
          <select value={cadence} disabled={busy !== null} onChange={(event) => setCadence(event.target.value as DeploymentCadence)}>
            <option value="scheduler">Scheduler default</option>
            <option value="manual">Manual only</option>
            <option value="15m">15 minutes</option>
            <option value="30m">30 minutes</option>
            <option value="60m">60 minutes</option>
            <option value="daily">Daily</option>
          </select>
        </label>
        <label className="field">
          <span>Exposure limit %</span>
          <input type="number" min={1} max={100} step={1} value={limit} disabled={busy !== null} onChange={(event) => setLimit(Number(event.target.value))} />
        </label>
        <button
          className="button compact"
          type="button"
          disabled={busy !== null || !Number.isFinite(limit) || limit <= 0 || limit > 100}
          onClick={() => run("save", () => updateDeploymentControls(deployment.id, { scan_cadence: cadence, max_simulated_exposure_pct: limit / 100 }), "Deployment controls saved.")}
        >
          {busy === "save" ? "Saving..." : "Save controls"}
        </button>
      </div>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}
