import type { EvidenceAlert } from "@/lib/api";

export type AlertSeverity = "info" | "warning" | "critical";

export type EvidenceNotificationSettings = {
  browser_notifications_enabled: boolean;
  alert_min_severity: AlertSeverity;
  notify_on_research_opportunity: boolean;
  notify_on_exit_risk: boolean;
  notify_on_scheduler_error: boolean;
  notify_on_stale_data: boolean;
};

export type EvidenceNotificationHistoryItem = {
  alert_id: number | "test";
  title: string;
  body: string;
  created_at: string;
};

export const EVIDENCE_NOTIFICATION_SETTINGS_KEY = "keftrade:evidence-notification-settings";
export const EVIDENCE_NOTIFICATION_SENT_KEY = "keftrade:evidence-notification-sent-alerts";
export const EVIDENCE_NOTIFICATION_HISTORY_KEY = "keftrade:evidence-notification-history";

export const DEFAULT_EVIDENCE_NOTIFICATION_SETTINGS: EvidenceNotificationSettings = {
  browser_notifications_enabled: false,
  alert_min_severity: "warning",
  notify_on_research_opportunity: true,
  notify_on_exit_risk: true,
  notify_on_scheduler_error: true,
  notify_on_stale_data: true,
};

const SEVERITY_RANK: Record<AlertSeverity, number> = {
  info: 1,
  warning: 2,
  critical: 3,
};

export function normalizeNotificationSettings(value: unknown): EvidenceNotificationSettings {
  if (!value || typeof value !== "object") {
    return DEFAULT_EVIDENCE_NOTIFICATION_SETTINGS;
  }
  const row = value as Partial<EvidenceNotificationSettings>;
  return {
    browser_notifications_enabled: Boolean(row.browser_notifications_enabled),
    alert_min_severity: isSeverity(row.alert_min_severity) ? row.alert_min_severity : DEFAULT_EVIDENCE_NOTIFICATION_SETTINGS.alert_min_severity,
    notify_on_research_opportunity: row.notify_on_research_opportunity ?? true,
    notify_on_exit_risk: row.notify_on_exit_risk ?? true,
    notify_on_scheduler_error: row.notify_on_scheduler_error ?? true,
    notify_on_stale_data: row.notify_on_stale_data ?? true,
  };
}

export function shouldNotifyAlert(alert: EvidenceAlert, settings: EvidenceNotificationSettings, sentAlertIds: Set<number>) {
  if (!settings.browser_notifications_enabled) return false;
  if (alert.acknowledged_at) return false;
  if (sentAlertIds.has(alert.id)) return false;
  if (SEVERITY_RANK[alert.severity] < SEVERITY_RANK[settings.alert_min_severity]) return false;
  if (alert.alert_type === "entry_setup_review" && !settings.notify_on_research_opportunity) return false;
  if (alert.alert_type === "exit_risk_review" && !settings.notify_on_exit_risk) return false;
  if (alert.alert_type === "scheduler_error" && !settings.notify_on_scheduler_error) return false;
  if (alert.alert_type === "stale_data_warning" && !settings.notify_on_stale_data) return false;
  return true;
}

export function notificationTitle(alert: EvidenceAlert) {
  if (alert.alert_type === "entry_setup_review") return `${alert.symbol} setup worth reviewing`;
  if (alert.alert_type === "exit_risk_review") return "Exit risk worth reviewing";
  if (alert.alert_type === "scheduler_error") return "Scheduler warning";
  if (alert.alert_type === "stale_data_warning") return "Data freshness warning";
  return alert.verdict === "Research Opportunity" ? "Research opportunity detected" : `${alert.symbol} evidence alert`;
}

export function notificationBody(alert: EvidenceAlert) {
  const mainReason = alert.matched_rules[0] || alert.failed_rules[0] || alert.evidence_summary;
  return [
    `${alert.symbol} ${alert.timeframe}`,
    `Strategy: ${alert.strategy_id}`,
    `Verdict: ${alert.verdict}`,
    `Evidence score: ${evidenceScore(alert)}`,
    `Reason: ${mainReason}`,
    "Research-only. No trade executed.",
  ].join("\n");
}

export function evidenceScore(alert: Pick<EvidenceAlert, "matched_rules" | "failed_rules">) {
  const matched = alert.matched_rules?.length ?? 0;
  const failed = alert.failed_rules?.length ?? 0;
  return `${matched}/${matched + failed}`;
}

function isSeverity(value: unknown): value is AlertSeverity {
  return value === "info" || value === "warning" || value === "critical";
}
