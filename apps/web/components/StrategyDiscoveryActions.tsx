"use client";

import { useState } from "react";
import { evolveStrategyDiscovery, runStrategyDiscovery, type StrategyDiscoveryRun } from "@/lib/api";
import { Toast } from "@/components/ResearchUI";

export function StrategyDiscoveryActions() {
  const [loading, setLoading] = useState<"run" | "evolve" | null>(null);
  const [message, setMessage] = useState("");
  const [latestRun, setLatestRun] = useState<StrategyDiscoveryRun | null>(null);

  async function runDiscovery() {
    setLoading("run");
    setMessage("");
    try {
      const result = await runStrategyDiscovery({ symbol: "BTCUSDT", timeframe: "4h", maxCandidates: 40 });
      setLatestRun(result);
      setMessage(`Discovery run ${result.run_id} evaluated ${result.evaluated} strategies; promoted ${result.promoted}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Discovery run failed.");
    } finally {
      setLoading(null);
    }
  }

  async function evolve() {
    setLoading("evolve");
    setMessage("");
    try {
      const result = await evolveStrategyDiscovery(20);
      setMessage(`Evolution generated ${String(result.variants_generated ?? 0)} child variants from stored promoted strategies.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Evolution failed.");
    } finally {
      setLoading(null);
    }
  }

  return (
    <div className="toolbar">
      <button className="button" type="button" onClick={runDiscovery} disabled={loading !== null}>
        {loading === "run" ? "Running..." : "Run discovery"}
      </button>
      <button className="button ghost" type="button" onClick={evolve} disabled={loading !== null}>
        {loading === "evolve" ? "Evolving..." : "Evolve promoted"}
      </button>
      {message ? <Toast tone={message.includes("failed") || message.includes("Error") ? "error" : "info"} message={message} /> : null}
      {latestRun ? <span className="formHint">Research-only: {latestRun.safety}</span> : null}
    </div>
  );
}
