"use client";

import { useCallback, useEffect, useState } from "react";
import { Archive, Layers, RefreshCw, Sparkles, Zap } from "lucide-react";
import {
  getFamilyRegistry,
  launchHighFrequencyCampaign,
  launchHiddenGemRecovery,
  refreshFamilyRegistry,
  type FamilyRegistryRow
} from "@/lib/api";

const ACTIVE_CLASS_TONE: Record<string, string> = {
  "Excellent": "good",
  "Good: promising, under-promoted": "good",
  "Too restrictive": "warn"
};

function tone(row: FamilyRegistryRow): string {
  if (row.status === "legacy") return "muted";
  return ACTIVE_CLASS_TONE[row.classification] ?? "warn";
}

function shortClass(classification: string): string {
  return classification
    .replace("Good: promising, under-promoted", "Promising")
    .replace("Retire: dead (never trades)", "Dead")
    .replace("Retire: negative edge", "Negative edge")
    .replace("Broken elite: median unprofitable", "Broken elite")
    .replace("Redesign: weak edge", "Weak edge");
}

function num(value: number | null, digits = 2): string {
  return value == null ? "—" : Number(value).toFixed(digits);
}

export function StrategyLibraryPanel() {
  const [active, setActive] = useState<FamilyRegistryRow[] | null>(null);
  const [legacyCount, setLegacyCount] = useState<number | null>(null);
  const [legacyRows, setLegacyRows] = useState<FamilyRegistryRow[] | null>(null);
  const [showLegacy, setShowLegacy] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [activeRes, legacyRes] = await Promise.all([getFamilyRegistry("active"), getFamilyRegistry("legacy")]);
      setActive(activeRes.families);
      setLegacyRows(legacyRes.families);
      setLegacyCount(legacyRes.count);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load the strategy library.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(action: string, operation: () => Promise<unknown>, describe: (result: any) => string) {
    setBusy(action);
    setError(null);
    setNotice(null);
    try {
      const result = await operation();
      setNotice(describe(result));
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The action failed.");
    } finally {
      setBusy(null);
    }
  }

  const activeCount = active?.length ?? 0;

  return (
    <section className="strategyLibrary" aria-labelledby="strategy-library-title">
      <header className="strategyLibraryHeader">
        <div>
          <span className="eyebrow"><Layers size={13} /> Strategy library</span>
          <h2 id="strategy-library-title">What research is allowed to spend compute on</h2>
          <p>Every strategy family is classified from its own evidence. Only productive families stay active; the rest are archived — their evidence is preserved, but no new campaign tests them.</p>
        </div>
        <div className="strategyLibrarySummary">
          <span><strong>{activeCount}</strong> active</span>
          <span className="muted"><strong>{legacyCount ?? "—"}</strong> archived</span>
          <button className="campaignIconButton" type="button" onClick={() => void run("refresh", refreshFamilyRegistry, (r) => `Re-audited ${r.families} families: ${r.active} active, ${r.legacy} archived.`)} disabled={Boolean(busy)} title="Re-audit every family from evidence" aria-label="Refresh strategy library">
            <RefreshCw size={15} className={busy === "refresh" ? "isSpinning" : undefined} />
          </button>
        </div>
      </header>

      {error ? <div className="strategyLibraryError" role="alert">{error}</div> : null}
      {notice ? <div className="strategyLibraryNotice">{notice}</div> : null}

      <div className="strategyLaunchRow">
        <button className="button secondary" type="button" disabled={Boolean(busy)} onClick={() => void run("hf", () => launchHighFrequencyCampaign(120), (r) => `High-frequency campaign #${r.campaign_id} queued (${r.jobs_created} jobs on ${(r.timeframes || []).join(", ")}).`)}>
          <Zap size={15} /> {busy === "hf" ? "Launching…" : "New: high-frequency campaign"}
        </button>
        <button className="button secondary" type="button" disabled={Boolean(busy)} onClick={() => void run("gem", () => launchHiddenGemRecovery(27), (r) => `Hidden-gem recovery #${r.campaign_id} queued across ${(r.families || []).length} families.`)}>
          <Sparkles size={15} /> {busy === "gem" ? "Launching…" : "New: hidden-gem recovery"}
        </button>
      </div>

      <div className="strategyFamilyTable" role="table" aria-label="Active strategy families">
        <div role="row" className="strategyFamilyHead">
          <span role="columnheader">Family</span>
          <span role="columnheader">Class</span>
          <span role="columnheader">Med PF</span>
          <span role="columnheader">Trades</span>
          <span role="columnheader">Elites</span>
        </div>
        {active === null ? <div className="strategyFamilyEmpty">Loading families…</div> : null}
        {active && active.length === 0 ? <div className="strategyFamilyEmpty">No active families. Run a re-audit after your next campaign.</div> : null}
        {(active ?? []).map((row) => (
          <div role="row" key={row.family_id} className={`strategyFamilyRow ${tone(row)}`}>
            <span role="cell" className="mono">{row.family_id}</span>
            <span role="cell"><em className={`familyTag ${tone(row)}`}>{shortClass(row.classification)}</em></span>
            <span role="cell" className="mono">{num(row.median_profit_factor)}</span>
            <span role="cell" className="mono">{num(row.avg_trades, 0)}</span>
            <span role="cell" className="mono">{row.elites}</span>
          </div>
        ))}
      </div>

      <div className="strategyLegacy">
        <button type="button" className="strategyLegacyToggle" onClick={() => setShowLegacy((prev) => !prev)} aria-expanded={showLegacy}>
          <Archive size={14} /> {showLegacy ? "Hide" : "Show"} {legacyCount ?? 0} archived families
        </button>
        {showLegacy ? (
          <div className="strategyFamilyTable legacy" role="table" aria-label="Archived strategy families">
            {(legacyRows ?? []).slice(0, 40).map((row) => (
              <div role="row" key={row.family_id} className="strategyFamilyRow muted">
                <span role="cell" className="mono">{row.family_id}</span>
                <span role="cell"><em className="familyTag muted">{shortClass(row.classification)}</em></span>
                <span role="cell" className="mono">{num(row.median_profit_factor)}</span>
                <span role="cell" className="mono">{num(row.avg_trades, 0)}</span>
                <span role="cell" className="mono">{row.elites}</span>
              </div>
            ))}
            {(legacyRows?.length ?? 0) > 40 ? <div className="strategyFamilyEmpty">Showing 40 of {legacyCount}. All evidence is preserved and queryable.</div> : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}
