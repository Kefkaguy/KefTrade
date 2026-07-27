"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Check, LoaderCircle, LockKeyhole, Play, RefreshCw, ShieldCheck } from "lucide-react";
import {
  approveMemberForAlpacaPaper,
  enableMemberPaperExecution,
  getPortfolioActivation,
  type ActivationMember,
  type PortfolioActivationView,
  type PreflightCheck
} from "@/lib/api";

/**
 * Step 04. Every state change here calls the same service function the CLI
 * calls, so the guards, audit trail and epochs are identical whichever entry
 * point is used. Nothing on this page can reach live money, and no step happens
 * implicitly: internal activation, Alpaca Paper approval, and order-submission
 * approval are three separate explicit actions.
 */
export function EliteActivationWorkspace({ portfolioId }: { portfolioId: number }) {
  const [view, setView] = useState<PortfolioActivationView | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      setView(await getPortfolioActivation(portfolioId));
    } catch (reason) {
      setError(message(reason));
    }
  }, [portfolioId]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function act(key: string, operation: () => Promise<unknown>) {
    setBusy(key);
    setError(null);
    try {
      await operation();
      await refresh();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
      setConfirming(null);
    }
  }

  if (!view) {
    return (
      <section className="eliteActivation loading">
        <LoaderCircle className="spin" size={20} />
        <span>{error ?? "Reading deployment state…"}</span>
      </section>
    );
  }

  const summary = view.summary;
  return (
    <section className="eliteActivation">
      <header className="eliteActivationHeader">
        <div>
          <span className="eyebrow">Step 4 · Activation</span>
          <h2>Take the approved portfolio to Alpaca Paper</h2>
          <p>
            Three explicit approvals, in order: activate internal deployments, approve each member for Alpaca Paper in
            observe-only state, then authorise order submission once its preflight passes. Nothing here can reach live
            money.
          </p>
        </div>
        <div className="eliteActivationIdentity">
          <div><span>Portfolio run</span><strong>#{view.portfolio_run_id}</strong></div>
          <div><span>Status</span><strong>{title(view.status)}</strong></div>
          <div><span>Profile</span><strong>{view.profile ? title(view.profile) : "—"}</strong></div>
          <div><span>Approved snapshot</span><code>{(view.approved_snapshot_hash ?? view.snapshot_hash ?? "—").slice(0, 16)}</code></div>
        </div>
      </header>

      {error ? (
        <div className="eliteValidationNotice error">
          <AlertTriangle size={16} />
          <span><strong>Action stopped</strong>{error}</span>
        </div>
      ) : null}

      <div className="eliteChampionMetrics">
        <Metric label="Members" value={summary.member_count} />
        <Metric label="Internally active" value={summary.internally_active} />
        <Metric label="Observe only" value={summary.observe_only} />
        <Metric label="Preflight ready" value={summary.preflight_ready} />
        <Metric label="Execution enabled" value={summary.execution_enabled} tone={summary.execution_enabled ? "safe" : undefined} />
      </div>

      <SafetyPanel view={view} />

      <div className="eliteActivationMembers">
        {view.members.map((member) => (
          <MemberCard
            key={member.id}
            member={member}
            busy={busy}
            confirming={confirming === member.id}
            onApprove={() => act(`approve-${member.id}`, () => approveMemberForAlpacaPaper(portfolioId, member.id))}
            onRequestExecution={() => setConfirming(member.id)}
            onCancelExecution={() => setConfirming(null)}
            onConfirmExecution={() => act(`execute-${member.id}`, () => enableMemberPaperExecution(portfolioId, member.id))}
          />
        ))}
      </div>

      <button className="eliteTextButton" onClick={() => void refresh()} disabled={Boolean(busy)}>
        <RefreshCw size={14} />Refresh deployment state
      </button>
    </section>
  );
}

function SafetyPanel({ view }: { view: PortfolioActivationView }) {
  const safety = view.safety;
  const limits = safety.risk_limits;
  const halts = safety.active_halts ?? [];
  return (
    <div className="eliteSafetyPanel">
      <h3>Before any order is submitted</h3>
      <div className="eliteSafetyGrid">
        <SafetyRow label="Alpaca account" value={`${safety.provider} · ${safety.environment}`} ok={safety.account_is_paper} detail="Paper account, not live" />
        <SafetyRow label="Broker sync" value={safety.broker_sync ? String((safety.broker_sync as any).status ?? "complete") : "none"} ok={Boolean(safety.broker_sync)} detail="A completed sync is required" />
        <SafetyRow label="Reconciliation" value={safety.reconciliation ? String((safety.reconciliation as any).status) : "none"} ok={(safety.reconciliation as any)?.status === "clean"} detail="Must be clean" />
        <SafetyRow label="Market clock" value={(safety.market_clock as any)?.is_open === undefined ? "unknown" : (safety.market_clock as any).is_open ? "open" : "closed"} ok detail="Informational" />
        <SafetyRow label="Active halts" value={String(halts.length)} ok={halts.length === 0} detail={halts.length ? halts.map((row: any) => row.reason_code).join(", ") : "None"} />
      </div>
      <div className="eliteRiskLimits">
        <div><span>Allocated capital</span><strong>{String(limits.allocated_capital)}</strong></div>
        <div><span>Max risk / trade</span><strong>{String(limits.max_risk_per_trade_pct)}%</strong></div>
        <div><span>Max total exposure</span><strong>{String(limits.max_total_exposure_pct)}%</strong></div>
        <div><span>Max open positions</span><strong>{String(limits.max_open_positions)}</strong></div>
        <div><span>Daily loss limit</span><strong>{String(limits.daily_loss_limit_pct)}%</strong></div>
        <div><span>Weekly loss limit</span><strong>{String(limits.weekly_loss_limit_pct)}%</strong></div>
      </div>
      <small className="eliteProfilePolicy">
        <LockKeyhole size={12} /> Live money supported: {String(safety.live_money_supported)}. Long-only enforced:{" "}
        {String(limits.long_only)}.
      </small>
    </div>
  );
}

function SafetyRow({ label, value, ok, detail }: { label: string; value: string; ok: boolean; detail: string }) {
  return (
    <div className={`eliteSafetyRow ${ok ? "ok" : "warn"}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function MemberCard({
  member,
  busy,
  confirming,
  onApprove,
  onRequestExecution,
  onCancelExecution,
  onConfirmExecution
}: {
  member: ActivationMember;
  busy: string | null;
  confirming: boolean;
  onApprove: () => void;
  onRequestExecution: () => void;
  onCancelExecution: () => void;
  onConfirmExecution: () => void;
}) {
  const actions = Object.fromEntries(member.available_actions.map((row) => [row.action, row]));
  const approve = actions.approve_external_paper;
  const execute = actions.enable_paper_execution;
  const preflight = member.preflight;
  const running = busy === `approve-${member.id}` || busy === `execute-${member.id}`;
  return (
    <article className={`eliteActivationMember state-${member.external_deployment_state}`}>
      <header>
        <div>
          <strong>{member.symbol} · {member.timeframe}</strong>
          <small>{member.strategy_family ?? member.family_id ?? "—"} · {member.strategy_direction} · {member.candidate_id}</small>
        </div>
        <em className="eliteStateChip">{member.external_deployment_state_label}</em>
      </header>

      <div className="eliteMemberDeployments">
        <div><span>Internal deployment</span><strong>{member.internal_deployment_id ? `#${member.internal_deployment_id} · ${member.internal_deployment_state}` : "not created"}</strong></div>
        <div><span>External deployment</span><strong>{member.external_deployment_id ? `#${member.external_deployment_id}` : "not created"}</strong></div>
        {member.latest_error ? <div className="error"><span>Last error</span><strong>{member.latest_error}</strong></div> : null}
      </div>

      {preflight ? <PreflightChecklist checks={preflight.checks} passed={preflight.passed} nextAction={preflight.next_action} /> : null}

      <MemberActivity member={member} />

      <div className="eliteMemberActions">
        {approve ? (
          <button className="button secondary" disabled={!approve.enabled || running} onClick={onApprove} title={approve.reason}>
            {busy === `approve-${member.id}` ? <LoaderCircle className="spin" size={15} /> : <ShieldCheck size={15} />}
            Approve for Alpaca Paper
          </button>
        ) : null}
        {execute && !confirming ? (
          <button className="button" disabled={!execute.enabled || running} onClick={onRequestExecution} title={execute.reason}>
            <Play size={15} />Enable Alpaca Paper execution
          </button>
        ) : null}
        {confirming ? (
          <div className="eliteExecutionConfirm">
            <strong>Authorise Alpaca Paper order submission for {member.symbol} {member.timeframe}?</strong>
            <span>This is the last approval before real orders reach the broker. Paper money only — live money is never reachable from this workflow.</span>
            <div>
              <button className="button" disabled={running} onClick={onConfirmExecution}>
                {busy === `execute-${member.id}` ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}Confirm
              </button>
              <button className="eliteTextButton" onClick={onCancelExecution}>Cancel</button>
            </div>
          </div>
        ) : null}
      </div>
      {!approve?.enabled && !execute?.enabled && !confirming ? (
        <small className="eliteMemberBlockedReason">{execute?.reason ?? approve?.reason}</small>
      ) : null}
    </article>
  );
}

function PreflightChecklist({ checks, passed, nextAction }: { checks: PreflightCheck[]; passed: boolean; nextAction: string }) {
  return (
    <div className={`elitePreflight ${passed ? "ready" : ""}`}>
      <h4>
        Execution preflight
        <em>{passed ? "All checks pass" : `Next: ${nextAction}`}</em>
      </h4>
      <p className="elitePreflightNote">
        Automated transition checks. The observe-only runner produces the decision records on its own as it evaluates
        bars — there is no separate shadow-trading workspace to operate.
      </p>
      <ul>
        {checks.map((check) => (
          <li key={check.code} className={check.passed ? "passed" : "pending"}>
            {check.passed ? <Check size={12} /> : <AlertTriangle size={12} />}
            <span><strong>{check.label}</strong><small>{check.detail}</small></span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function MemberActivity({ member }: { member: ActivationMember }) {
  const activity = member.activity;
  const rows: Array<[string, unknown]> = [
    ["Last scan", stamp(activity.last_scan)],
    ["Last signal", stamp(activity.last_signal)],
    ["Last risk decision", stamp(activity.last_risk_decision)],
    ["Last proposed order", stamp(activity.last_proposed_order)],
    ["Last submitted order", orderSummary(activity.last_submitted_order)],
    ["Last fill", fillSummary(activity.last_fill)],
    ["Halt reason", activity.halt_reason ?? "—"]
  ];
  if (rows.every(([, value]) => value === "—")) return null;
  return (
    <div className="eliteMemberActivity">
      {rows.map(([label, value]) => (
        <div key={label}><span>{label}</span><strong>{String(value)}</strong></div>
      ))}
    </div>
  );
}

function stamp(row: Record<string, any> | null): string {
  if (!row) return "—";
  const value = row.created_at ?? row.transaction_at ?? row.submitted_at;
  return value ? new Date(String(value)).toLocaleString() : "recorded";
}

function orderSummary(row: Record<string, any> | null): string {
  if (!row) return "—";
  const parts = [row.status, row.symbol, row.requested_quantity && `x${row.requested_quantity}`].filter(Boolean);
  return parts.length ? parts.join(" · ") : "recorded";
}

function fillSummary(row: Record<string, any> | null): string {
  if (!row) return "—";
  return [row.side, row.quantity && `x${row.quantity}`, row.price && `@${row.price}`].filter(Boolean).join(" ") || "recorded";
}

function Metric({ label, value, tone }: { label: string; value: unknown; tone?: string }) {
  return <div className={`eliteMetric ${tone ?? ""}`}><span>{label}</span><strong>{String(value)}</strong></div>;
}

function title(value: string) {
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function message(reason: unknown) {
  return reason instanceof Error ? reason.message : "The activation step failed.";
}
