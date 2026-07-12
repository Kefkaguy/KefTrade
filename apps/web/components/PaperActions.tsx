"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { acknowledgeEvidenceAlert, addSignalReviewNote, cancelPaperOrder, createPaperAccount, createPaperOrder, createStrategyDeployment, deployTslaMomentumBull, generateSignalReview, ignoreSignalReview, markSignalReviewReviewed, pauseStrategyDeployment, processPendingPaperOrders, reconcilePaperAccount, scanStrategyDeployment, sendSignalReviewToPaperSimulation, updatePaperScheduler, type EvidenceAlert, type SignalReview } from "@/lib/api";
import {
  DEFAULT_EVIDENCE_NOTIFICATION_SETTINGS,
  EVIDENCE_NOTIFICATION_HISTORY_KEY,
  EVIDENCE_NOTIFICATION_SENT_KEY,
  EVIDENCE_NOTIFICATION_SETTINGS_KEY,
  notificationBody,
  notificationTitle,
  normalizeNotificationSettings,
  shouldNotifyAlert,
  type AlertSeverity,
  type EvidenceNotificationHistoryItem,
  type EvidenceNotificationSettings
} from "@/lib/evidence-notifications";
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

export function AcknowledgeAlertButton({ alertId }: { alertId: number }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  return (
    <button
      className="button compact ghost"
      type="button"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await acknowledgeEvidenceAlert(alertId);
          router.refresh();
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy ? "Acknowledging..." : "Acknowledge"}
    </button>
  );
}

export function EvidenceNotificationControls({ alerts }: { alerts: EvidenceAlert[] }) {
  const [settings, setSettings] = useState<EvidenceNotificationSettings>(DEFAULT_EVIDENCE_NOTIFICATION_SETTINGS);
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">("default");
  const [history, setHistory] = useState<EvidenceNotificationHistoryItem[]>([]);
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });

  useEffect(() => {
    if (typeof window === "undefined") return;
    setPermission("Notification" in window ? Notification.permission : "unsupported");
    setSettings(readSettings());
    setHistory(readHistory());
  }, []);

  useEffect(() => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (Notification.permission !== "granted") return;
    const sent = readSentAlertIds();
    const nextHistory = readHistory();
    let changed = false;
    for (const alert of alerts) {
      if (!shouldNotifyAlert(alert, settings, sent)) continue;
      const title = notificationTitle(alert);
      const body = notificationBody(alert);
      new Notification(title, { body, tag: `keftrade-alert-${alert.id}` });
      sent.add(alert.id);
      nextHistory.unshift({ alert_id: alert.id, title, body, created_at: new Date().toISOString() });
      changed = true;
    }
    if (changed) {
      writeSentAlertIds(sent);
      writeHistory(nextHistory.slice(0, 20));
      setHistory(nextHistory.slice(0, 20));
    }
  }, [alerts, settings]);

  async function enableNotifications() {
    if (typeof window === "undefined" || !("Notification" in window)) {
      setPermission("unsupported");
      setToast({ tone: "error", message: "Browser notifications are not supported here." });
      return;
    }
    const result = await Notification.requestPermission();
    setPermission(result);
    const next = { ...settings, browser_notifications_enabled: result === "granted" };
    saveSettings(next);
    setToast({ tone: result === "granted" ? "success" : "error", message: result === "granted" ? "Browser notifications enabled." : "Notification permission was not granted." });
  }

  function saveSettings(next: EvidenceNotificationSettings) {
    setSettings(next);
    if (typeof window !== "undefined") {
      localStorage.setItem(EVIDENCE_NOTIFICATION_SETTINGS_KEY, JSON.stringify(next));
    }
  }

  function updateSetting<K extends keyof EvidenceNotificationSettings>(key: K, value: EvidenceNotificationSettings[K]) {
    saveSettings({ ...settings, [key]: value });
  }

  function sendTestNotification() {
    if (typeof window === "undefined" || !("Notification" in window) || Notification.permission !== "granted") {
      setToast({ tone: "error", message: "Enable browser notification permission first." });
      return;
    }
    const title = "Research opportunity detected";
    const body = "TSLA 1h\nStrategy: momentum_bull_v2\nVerdict: Setup Worth Reviewing\nEvidence score: 4/5\nReason: Notification test.\nResearch-only. No trade executed.";
    new Notification(title, { body, tag: "keftrade-alert-test" });
    const testHistoryItem: EvidenceNotificationHistoryItem = { alert_id: "test", title, body, created_at: new Date().toISOString() };
    const nextHistory: EvidenceNotificationHistoryItem[] = [testHistoryItem, ...history].slice(0, 20);
    writeHistory(nextHistory);
    setHistory(nextHistory);
    setToast({ tone: "success", message: "Sent research-only test notification." });
  }

  return (
    <div className="workflowStack">
      <div className="operationBar">
        <button className="button" type="button" onClick={enableNotifications}>{settings.browser_notifications_enabled ? "Refresh permission" : "Enable notifications"}</button>
        <button className="button ghost" type="button" onClick={() => updateSetting("browser_notifications_enabled", false)}>Disable notifications</button>
        <button className="button secondary" type="button" onClick={sendTestNotification}>Test notification</button>
        <Toast tone={toast.tone} message={toast.message} />
      </div>
      <div className="scoreList">
        <span>Browser permission <strong>{permission}</strong></span>
        <span>Notifications enabled <strong>{settings.browser_notifications_enabled ? "Yes" : "No"}</strong></span>
      </div>
      <label className="field">
        <span>Minimum severity</span>
        <select value={settings.alert_min_severity} onChange={(event) => updateSetting("alert_min_severity", event.target.value as AlertSeverity)}>
          <option value="info">Info</option>
          <option value="warning">Warning</option>
          <option value="critical">Critical</option>
        </select>
      </label>
      <div className="metadataGrid">
        <Toggle label="Research opportunities" checked={settings.notify_on_research_opportunity} onChange={(checked) => updateSetting("notify_on_research_opportunity", checked)} />
        <Toggle label="Exit risk" checked={settings.notify_on_exit_risk} onChange={(checked) => updateSetting("notify_on_exit_risk", checked)} />
        <Toggle label="Scheduler errors" checked={settings.notify_on_scheduler_error} onChange={(checked) => updateSetting("notify_on_scheduler_error", checked)} />
        <Toggle label="Stale data" checked={settings.notify_on_stale_data} onChange={(checked) => updateSetting("notify_on_stale_data", checked)} />
      </div>
      <div className="actionNote">
        <strong>Notification history</strong>
        {history.length ? (
          <div className="list">
            {history.slice(0, 6).map((item) => <span key={`${item.alert_id}-${item.created_at}`}>{item.title} — {new Date(item.created_at).toLocaleString()}</span>)}
          </div>
        ) : <p>No browser notifications sent from this browser yet.</p>}
      </div>
    </div>
  );
}

export function SignalReviewControls({ review, deploymentId }: { review?: SignalReview | null; deploymentId?: number }) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState(review?.note ?? "");
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string }>({ tone: "info", message: "" });

  async function runAction(action: string, task: () => Promise<unknown>, success: string) {
    setBusy(action);
    try {
      await task();
      setToast({ tone: "success", message: success });
      router.refresh();
    } catch (error) {
      setToast({ tone: "error", message: error instanceof Error ? error.message : "Signal review action failed." });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="workflowStack">
      <div className="operationBar">
        <button
          className="button"
          type="button"
          disabled={busy !== null || !deploymentId}
          onClick={() => deploymentId ? runAction("refresh", () => generateSignalReview(deploymentId), "Signal Review refreshed from latest stored evidence.") : undefined}
        >
          {busy === "refresh" ? "Refreshing..." : "Refresh Signal Review"}
        </button>
        <button className="button ghost" type="button" disabled={busy !== null || !review} onClick={() => review ? runAction("reviewed", () => markSignalReviewReviewed(review.id), "Marked reviewed.") : undefined}>Mark Reviewed</button>
        <button className="button ghost" type="button" disabled={busy !== null || !review} onClick={() => review ? runAction("ignored", () => ignoreSignalReview(review.id), "Setup ignored for review purposes.") : undefined}>Ignore Setup</button>
        <button className="button secondary" type="button" disabled={busy !== null || !review} onClick={() => review ? runAction("sent", () => sendSignalReviewToPaperSimulation(review.id), "Sent to internal paper simulation queue. No order was created.") : undefined}>Send to Internal Paper Simulation</button>
      </div>
      <div className="formGrid">
        <label className="field">
          <span>Add Note</span>
          <textarea value={note} rows={3} placeholder="Research-only note for this setup review." onChange={(event) => setNote(event.target.value)} />
        </label>
        <button className="button compact" type="button" disabled={busy !== null || !review || !note.trim()} onClick={() => review ? runAction("note", () => addSignalReviewNote(review.id, note.trim()), "Signal Review note saved.") : undefined}>Save Note</button>
      </div>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label>
      <span>{label}</span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  );
}

function readSettings() {
  try {
    return normalizeNotificationSettings(JSON.parse(localStorage.getItem(EVIDENCE_NOTIFICATION_SETTINGS_KEY) || "null"));
  } catch {
    return DEFAULT_EVIDENCE_NOTIFICATION_SETTINGS;
  }
}

function readSentAlertIds() {
  try {
    const values = JSON.parse(localStorage.getItem(EVIDENCE_NOTIFICATION_SENT_KEY) || "[]");
    return new Set<number>(Array.isArray(values) ? values.map(Number).filter(Number.isFinite) : []);
  } catch {
    return new Set<number>();
  }
}

function writeSentAlertIds(values: Set<number>) {
  localStorage.setItem(EVIDENCE_NOTIFICATION_SENT_KEY, JSON.stringify(Array.from(values)));
}

function readHistory() {
  try {
    const values = JSON.parse(localStorage.getItem(EVIDENCE_NOTIFICATION_HISTORY_KEY) || "[]");
    return Array.isArray(values) ? values as EvidenceNotificationHistoryItem[] : [];
  } catch {
    return [];
  }
}

function writeHistory(values: EvidenceNotificationHistoryItem[]) {
  localStorage.setItem(EVIDENCE_NOTIFICATION_HISTORY_KEY, JSON.stringify(values));
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
