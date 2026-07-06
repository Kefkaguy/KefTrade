"use client";

import { useState } from "react";
import { generateSignal, syncCandles, syncFeatures } from "@/lib/api";

export function DataActions() {
  const [status, setStatus] = useState("Ready");

  async function syncAll() {
    setStatus("Syncing dev market data...");
    const candles = await syncCandles();
    setStatus(`Candles received: ${String(candles.received ?? 0)}. Calculating features...`);
    const features = await syncFeatures();
    setStatus(`Features usable: ${String(features.usable ?? 0)}. Generating signal...`);
    const signal = await generateSignal();
    setStatus(`Features usable: ${String(features.usable ?? 0)}. Signal: ${signal.signal}.`);
  }

  return (
    <div className="toolbar">
      <button className="button" onClick={syncAll}>
        Sync dev candles
      </button>
      <span className="muted">{status}</span>
    </div>
  );
}
