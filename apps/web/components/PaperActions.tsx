"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { cancelPaperOrder, createPaperAccount, createPaperOrder, createStrategyDeployment, deployTslaMomentumBull, pauseStrategyDeployment, processPendingPaperOrders, reconcilePaperAccount, scanStrategyDeployment, updatePaperScheduler } from "@/lib/api";
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
  const [orderType, setOrderType] = useState("market");
  const [limitPrice, setLimitPrice] = useState(0);
  const [stopLoss, setStopLoss] = useState(0);
  const [takeProfit, setTakeProfit] = useState(0);
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });
  async function submit() {
    const entryReference = orderType === "limit" ? limitPrice : 0;
    if (orderType === "limit" && limitPrice <= 0) {
      setToast({ tone: "error", message: "Enter a valid limit price." });
      return;
    }
    if (side === "buy" && entryReference > 0 && stopLoss > 0 && stopLoss >= entryReference) {
      setToast({ tone: "error", message: "Stop loss must be below the entry price." });
      return;
    }
    if (side === "buy" && entryReference > 0 && takeProfit > 0 && takeProfit <= entryReference) {
      setToast({ tone: "error", message: "Take profit must be above the entry price." });
      return;
    }
    try {
      const order = await createPaperOrder({ account_id: accountId, symbol, quantity, side, order_type: orderType, timeframe: symbol.endsWith("USDT") ? "4h" : "1d", ...(orderType === "limit" && limitPrice > 0 ? { limit_price: limitPrice } : {}), ...(side === "buy" && stopLoss > 0 ? { stop_loss_price: stopLoss } : {}), ...(side === "buy" && takeProfit > 0 ? { take_profit_price: takeProfit } : {}) });
      setToast({ tone: order.status === "rejected" ? "error" : "success", message: order.rejected_reason || `Paper order ${order.status}.` });
      router.refresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Could not submit paper order." });
    }
  }
  return (
    <div className="formGrid">
      <label className="field"><span>Symbol</span><input value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} /></label>
      <label className="field"><span>Side</span><select value={side} onChange={(event) => setSide(event.target.value)}>
        <option value="buy">Buy</option>
        <option value="sell">Sell</option>
      </select></label>
      <label className="field"><span>Order type</span><select value={orderType} onChange={(event) => setOrderType(event.target.value)}>
        <option value="market">Market</option>
        <option value="limit">Limit</option>
      </select></label>
      <label className="field"><span>Quantity</span><input type="number" min={0.0001} step={0.0001} value={quantity} onChange={(event) => setQuantity(Number(event.target.value))} /></label>
      {orderType === "limit" ? <label className="field"><span>Limit price</span><input placeholder="Price to enter" type="number" min={0.0001} step={0.01} value={limitPrice || ""} onChange={(event) => setLimitPrice(Number(event.target.value))} /></label> : null}
      {side === "buy" ? (
        <div className="protectiveGrid">
          <label><span>Stop loss</span><input type="number" min={0.0001} step={0.01} placeholder="Optional" value={stopLoss || ""} onChange={(event) => setStopLoss(Number(event.target.value))} /></label>
          <label><span>Take profit</span><input type="number" min={0.0001} step={0.01} placeholder="Optional" value={takeProfit || ""} onChange={(event) => setTakeProfit(Number(event.target.value))} /></label>
        </div>
      ) : null}
      <p className="formHint">Protective exits activate only after the buy fills. Stop loss must be below entry; take profit must be above entry.</p>
      <button className="button" type="button" onClick={submit}>Submit simulated order</button>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}

export function CancelOrderButton({ orderId }: { orderId: number }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  return <button className="button compact danger" type="button" disabled={busy} onClick={async () => { setBusy(true); try { await cancelPaperOrder(orderId); router.refresh(); } finally { setBusy(false); } }}>{busy ? "Canceling..." : "Cancel"}</button>;
}

export function PaperOperations({ accountId }: { accountId: number }) {
  const router = useRouter();
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });
  async function process() {
    try { const result = await processPendingPaperOrders(accountId); setToast({ tone: "success", message: `Processed ${result.processed}; filled ${result.filled}; ${result.pending} remain pending.` }); router.refresh(); }
    catch (error) { setToast({ tone: "error", message: error instanceof Error ? error.message : "Processing failed." }); }
  }
  async function reconcile(repair: boolean) {
    try { const result = await reconcilePaperAccount(accountId, repair); setToast({ tone: result.healthy || result.repaired ? "success" : "error", message: result.healthy ? "Ledger is healthy." : result.repaired ? `Repaired ${result.issue_count} ledger issue(s).` : `Found ${result.issue_count} ledger issue(s).` }); router.refresh(); }
    catch (error) { setToast({ tone: "error", message: error instanceof Error ? error.message : "Reconciliation failed." }); }
  }
  return <div className="operationBar"><button className="button" type="button" onClick={process}>Process pending</button><button className="button ghost" type="button" onClick={() => reconcile(false)}>Check ledger</button><button className="button ghost" type="button" onClick={() => reconcile(true)}>Repair drift</button><Toast tone={toast.tone} message={toast.message} /></div>;
}

export function TslaPaperScanControls({ accountId, deploymentId }: { accountId: number; deploymentId?: number }) {
  const router = useRouter();
  const [busy, setBusy] = useState<"deploy" | "scan" | null>(null);
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });

  async function deploy() {
    setBusy("deploy");
    try {
      const deployment = await deployTslaMomentumBull(accountId);
      setToast({ tone: "success", message: `Simulation deployment active: ${deployment.symbol} ${deployment.timeframe} ${deployment.strategy_name}_${deployment.strategy_version}.` });
      router.refresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Could not create TSLA paper deployment." });
    } finally {
      setBusy(null);
    }
  }

  async function scan() {
    if (!deploymentId) {
      setToast({ tone: "error", message: "Create the TSLA simulation deployment before scanning." });
      return;
    }
    setBusy("scan");
    try {
      const result = await scanStrategyDeployment(deploymentId);
      setToast({ tone: "success", message: result.message || `Paper scan ${result.action}.` });
      router.refresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Paper scan failed." });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="operationBar">
      <button className="button" type="button" disabled={busy !== null} onClick={deploy}>{busy === "deploy" ? "Deploying..." : "Deploy TSLA candidate"}</button>
      <button className="button secondary" type="button" disabled={busy !== null || !deploymentId} onClick={scan}>{busy === "scan" ? "Scanning..." : "Run paper scan"}</button>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}

export function PaperSchedulerControls({ enabled, cadence }: { enabled: boolean; cadence: "manual" | "15m" | "30m" | "60m" }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });

  async function save(next: { enabled?: boolean; cadence?: "manual" | "15m" | "30m" | "60m" }) {
    setBusy(true);
    try {
      const status = await updatePaperScheduler(next);
      setToast({ tone: "success", message: `Scheduler ${status.enabled ? "enabled" : "disabled"} / ${status.cadence}.` });
      router.refresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Could not update scheduler." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="operationBar">
      <button className="button ghost" type="button" disabled={busy} onClick={() => save({ enabled: !enabled })}>{enabled ? "Disable scheduler" : "Enable scheduler"}</button>
      <label className="field schedulerSelect">
        <span>Cadence</span>
        <select value={cadence} disabled={busy} onChange={(event) => save({ cadence: event.target.value as "manual" | "15m" | "30m" | "60m" })}>
          <option value="manual">Manual</option>
          <option value="15m">15 minutes</option>
          <option value="30m">30 minutes</option>
          <option value="60m">60 minutes</option>
        </select>
      </label>
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
