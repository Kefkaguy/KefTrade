"use client";

import { useState } from "react";
import { syncCandles, syncFeatures } from "@/lib/api";

export function DataActions() {
  const [status, setStatus] = useState("Ready");

  async function syncAll() {
    setStatus("Syncing Binance candles...");
    const candles = await syncCandles();
    setStatus(`Candles received: ${String(candles.received ?? 0)}. Calculating features...`);
    const features = await syncFeatures();
    setStatus(`Features usable: ${String(features.usable ?? 0)}.`);
  }

  return (
    <div className="toolbar">
      <button className="button" onClick={syncAll}>
        Sync BTCUSDT 4h
      </button>
      <span className="muted">{status}</span>
    </div>
  );
}

