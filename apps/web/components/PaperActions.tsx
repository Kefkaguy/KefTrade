"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createPaperAccount, createPaperOrder, createStrategyDeployment, pauseStrategyDeployment } from "@/lib/api";
import { Toast } from "@/components/ResearchUI";

export function CreatePaperAccount() {
  const router = useRouter();
  const [name, setName] = useState("Research Paper Account");
  const [cash, setCash] = useState(10000);
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });
  async function submit() {
    try {
      await createPaperAccount({ name, starting_cash: cash });
      setToast({ tone: "success", message: "Paper account created. Simulation only." });
      router.refresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Could not create paper account." });
    }
  }
  return (
    <div className="formGrid">
      <input value={name} onChange={(event) => setName(event.target.value)} />
      <input type="number" min={1} value={cash} onChange={(event) => setCash(Number(event.target.value))} />
      <button className="button" type="button" onClick={submit}>Create paper account</button>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}

export function CreatePaperOrder({ accountId }: { accountId: number }) {
  const router = useRouter();
  const [symbol, setSymbol] = useState("AAPL");
  const [quantity, setQuantity] = useState(1);
  const [side, setSide] = useState("buy");
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });
  async function submit() {
    try {
      const order = await createPaperOrder({ account_id: accountId, symbol, quantity, side, order_type: "market", timeframe: symbol.endsWith("USDT") ? "4h" : "1d" });
      setToast({ tone: order.status === "rejected" ? "error" : "success", message: order.rejected_reason || `Paper order ${order.status}.` });
      router.refresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Could not submit paper order." });
    }
  }
  return (
    <div className="formGrid">
      <input value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
      <select value={side} onChange={(event) => setSide(event.target.value)}>
        <option value="buy">Buy</option>
        <option value="sell">Sell</option>
      </select>
      <input type="number" min={0.0001} step={0.0001} value={quantity} onChange={(event) => setQuantity(Number(event.target.value))} />
      <button className="button" type="button" onClick={submit}>Submit simulated order</button>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}

export function CreateDeployment({ accountId }: { accountId: number }) {
  const router = useRouter();
  const [strategy, setStrategy] = useState("trend_pullback");
  const [symbol, setSymbol] = useState("AAPL");
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });
  async function submit() {
    try {
      await createStrategyDeployment({ account_id: accountId, strategy_name: strategy, symbol, timeframe: symbol.endsWith("USDT") ? "4h" : "1d" });
      setToast({ tone: "success", message: "Simulation-only deployment created." });
      router.refresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Could not create deployment." });
    }
  }
  return (
    <div className="formGrid">
      <input value={strategy} onChange={(event) => setStrategy(event.target.value)} />
      <input value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
      <button className="button" type="button" onClick={submit}>Create simulated deployment</button>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}

export function PauseDeploymentButton({ deploymentId }: { deploymentId: number }) {
  const router = useRouter();
  return (
    <button
      className="button compact ghost"
      type="button"
      onClick={async () => {
        await pauseStrategyDeployment(deploymentId);
        router.refresh();
      }}
    >
      Pause
    </button>
  );
}
