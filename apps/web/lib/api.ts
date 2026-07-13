const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type Candle = {
  timestamp: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
};

export type StrategyExperimentDefinition = {
  id: string;
  strategy: string;
  title: string;
  hypothesis: string;
  variables: string[];
  sweep: Record<string, unknown[]>;
  rationale: string;
};

export type Signal = {
  symbol: string;
  timeframe: string;
  strategy_name?: string;
  strategy_version?: string;
  signal: "setup" | "watchlist" | "avoid";
  generated_at?: string;
  entry_zone?: string[] | number[] | null;
  stop_loss?: string | number | null;
  take_profit?: string | number | null;
  risk_reward?: string | number | null;
  explanation: string[];
};

export type BacktestResult = {
  id: number;
  metrics: Record<string, unknown>;
  trades: Array<Record<string, unknown>>;
};

export type StrategyResearchRun = {
  run_id: string;
  rank: number;
  strategy_name: string;
  strategy_version: string;
  description: string;
  parameters: Record<string, unknown>;
  entry_rules: string[];
  exit_rules: string[];
  supported_market_regimes: string[];
  metrics: Record<string, unknown>;
  equity_curve_summary: Record<string, unknown>;
  trade_count: number;
  by_year: Array<{ year: number; metrics: Record<string, unknown> }>;
  by_volatility_regime: Array<{ regime: string; metrics: Record<string, unknown> }>;
  by_market_regime: Array<{ regime: string; metrics: Record<string, unknown> }>;
  by_trend_strength: Array<{ regime: string; metrics: Record<string, unknown> }>;
  feature_correlations: Array<{ feature: string; correlation_to_profitable_trade: number | null; sample_size: number }>;
  trade_explorer: Array<Record<string, unknown>>;
  filter_options: Record<string, string[]>;
  dashboard: Record<string, unknown>;
  paper_readiness?: {
    paper_ready?: boolean;
    failed_reasons?: string[];
    checks?: Array<{ name: string; passed: boolean; detail: string }>;
  };
  why_not_paper_ready?: string[];
  recommendation: "Reject" | "Needs More Research" | "Candidate for Paper Trading";
  markdown_report: string;
  rank_score: number;
};

export type StrategyResearchReport = {
  symbol: string;
  timeframe: string;
  strategy_name: string;
  strategy_version: string;
  run_count: number;
  rank_metrics: string[];
  strategy_library: Array<Record<string, unknown>>;
  ranking_table: StrategyResearchRun[];
  charts: Record<string, Array<{ run_id: string; rank: number; strategy_name: string; value: unknown }>>;
  dashboard: Record<string, unknown>;
  markdown_report: string;
};

export type AlphaDiscoveryRow = {
  rank: number;
  candidate_id: string;
  blocks: Record<string, unknown>;
  parameters: Record<string, unknown>;
  metrics: Record<string, unknown>;
  stability: Record<string, unknown>;
  monte_carlo: Record<string, unknown>;
  alpha_score: number;
  confidence_score: number;
  recommendation: "Reject" | "Research More" | "Candidate for Paper Trading";
  alpha_report: string;
};

export type AlphaDiscoveryReport = {
  symbol: string;
  timeframe: string;
  candidate_count: number;
  rank_metrics: string[];
  leaderboard: AlphaDiscoveryRow[];
  summary: Record<string, unknown>;
};

export type AlphaValidationRow = {
  rank: number;
  candidate_id: string;
  metrics: Record<string, unknown>;
  stability: Record<string, unknown>;
  robustness: Record<string, unknown>;
  market_results?: Array<Record<string, unknown>>;
  evidence_rules?: Record<string, boolean>;
  evidence_rule_details?: Record<
    string,
    {
      passed: boolean;
      actual: unknown;
      threshold: unknown;
      comparator: string;
      explanation: string;
    }
  >;
  passed_rules?: string[];
  failed_rules?: string[];
  rejection_explanation?: string;
  validation_score: number;
  recommendation: "Reject" | "Research More" | "Validated Alpha";
  markdown_report: string;
};

export type AlphaValidationReport = {
  id: number;
  symbols: string[];
  timeframes: string[];
  candidate_count: number;
  thresholds: Record<string, unknown>;
  summary: Record<string, unknown>;
  leaderboard: AlphaValidationRow[];
  markdown_report: string;
};

export type RiskSettings = {
  account_size: string;
  max_risk_per_trade: string;
  max_open_exposure: string;
  daily_loss_limit: string;
  weekly_loss_limit: string;
  allow_leverage: boolean;
  allow_live_trading: boolean;
};

export type CopilotResponse = {
  answer: string;
  evidence_refs: string[];
  confidence: string;
  model: string;
  safety_flags: string[];
  context_summary: Record<string, unknown>;
};

export type CopilotInteraction = {
  id: number;
  question: string;
  response: string;
  evidence_refs: string[];
  model: string;
  context_summary: Record<string, unknown>;
  safety_flags: string[];
  created_at: string;
};

export type SymbolRow = {
  symbol: string;
  asset_class: string;
  exchange: string;
  currency: string;
  name: string;
  provider_symbol: string;
  primary_provider: string;
  sector?: string | null;
  market_cap?: string | number | null;
  index_membership?: string[] | null;
  is_active: boolean;
};

export type ResearchHypothesis = {
  id: number;
  title: string;
  hypothesis: string;
  status: string;
  tags: string[];
  created_at: string;
  updated_at: string;
};

export type HypothesisPayload = {
  title: string;
  hypothesis: string;
  tags: string[];
};

export type ResearchJournalEntry = {
  id: number;
  hypothesis_id?: number | null;
  experiment_id?: number | null;
  entry_type: string;
  dataset: Record<string, unknown>;
  parameters: Record<string, unknown>;
  results: Record<string, unknown>;
  conclusion: string;
  next_actions: string[];
  created_at: string;
};

export type ResearchTimelineEvent = {
  timestamp: string | null;
  event_type: string;
  summary: string;
  evidence_refs: string[];
};

export type ResearchArchiveRow = {
  evidence_ref: string;
  candidate_id: string;
  strategy: string;
  indicators: string[];
  assets: string[];
  timeframes: string[];
  market_regimes: string[];
  recommendation: string;
  failure_reasons: string[];
  validation_status: string;
  metrics: Record<string, unknown>;
};

export type ValidationRun = {
  id: number;
  symbol_set: string[];
  timeframe_set: string[];
  candidate_count: number;
  thresholds: Record<string, unknown>;
  summary: Record<string, unknown>;
  created_at: string;
};

export type ValidationRunDetail = ValidationRun & {
  report: AlphaValidationReport;
  markdown_report: string;
};

export type ResearchIntelligence = {
  summary: {
    hypothesis_count: number;
    experiment_count: number;
    validation_run_count: number;
    evidence_item_count: number;
    recommendation_count: number;
    candidates_ranked?: number;
    high_quality_evidence_count?: number;
    strong_candidate_count?: number;
    incomplete_evidence_count?: number;
    rejected_or_weak_count?: number;
    active_setup_count?: number;
    stale_candidate_count?: number;
    average_research_score?: number;
    top_ranked_asset?: string | null;
    top_ranked_strategy?: string | null;
  };
  rankings: Array<Record<string, any>>;
  review_priorities: Array<Record<string, any>>;
  strategy_leaderboard: Array<Record<string, any>>;
  asset_leaderboard: Array<Record<string, any>>;
  candidate_comparisons: Array<Record<string, any>>;
  portfolio_intelligence: Record<string, any>;
  score_methodology: Record<string, any>;
  safety: Record<string, any>;
  subsystem_errors: Array<{ subsystem: string; error: string }>;
  meta_analysis: Record<string, Array<Record<string, unknown>>>;
  recommendations: Array<{
    title: string;
    finding: string;
    recommendation: string;
    evidence_refs: string[];
    confidence: string;
  }>;
  confidence: Array<{
    conclusion: string;
    confidence: string;
    supporting_evidence_count: number;
    evidence_refs: string[];
  }>;
  timeline: ResearchTimelineEvent[];
  archive: ResearchArchiveRow[];
  markdown_report: string;
};

export type PromisingResearchCandidate = {
  rank: number;
  candidate_id: string;
  experiment_id: string;
  strategy_name: string;
  title: string;
  parameters: Record<string, unknown>;
  aggregate_metrics: Record<string, unknown>;
  research_score: number;
  stability_score: number;
  cross_asset_consistency: number;
  timeframe_consistency: number;
  out_of_sample_score: number;
  dataset_results: Array<Record<string, unknown>>;
  train_test_results: Array<Record<string, unknown>>;
  walk_forward: Record<string, unknown>;
  assets_worked: string[];
  assets_failed: string[];
  validation_status: string;
  evidence_summary: string;
  recommended_next_experiment: string;
  research_report: string;
};

export type PromisingResearchReport = {
  summary: Record<string, unknown>;
  datasets: Array<Record<string, unknown>>;
  thresholds: Record<string, unknown>;
  rank_metrics: string[];
  candidates: PromisingResearchCandidate[];
  markdown_report: string;
};

export type MetricDefinition = {
  label: string;
  measures: string;
  why_it_matters: string;
  calculation: string;
};

export type LifecycleEvent = {
  id?: number;
  candidate_id: string;
  from_state?: string | null;
  to_state: string;
  reason: string;
  metrics: Record<string, unknown>;
  created_at: string;
};

export type EvidenceDrift = {
  status: string;
  score_delta: number;
  robustness_delta: number;
  message: string;
};

export type LifecycleCandidate = PromisingResearchCandidate & {
  lifecycle_status: string;
  lifecycle_events: LifecycleEvent[];
  evidence_drift: EvidenceDrift;
  research_notebook: string;
};

export type PortfolioTimelineEvent = {
  timestamp: string;
  candidate_id: string;
  event_type: string;
  summary: string;
  reason: string;
};

export type CandidateComparisonRow = {
  candidate_id: string;
  strategy: string;
  profit_factor: number | null;
  stability: number;
  trade_count: number;
  drawdown: number | null;
  research_score: number;
  assets: string[];
  timeframes: string[];
  validation_status: string;
  lifecycle_status: string;
};

export type EvidenceCluster = {
  cluster: string;
  candidate_count: number;
  avg_score: number;
  top_candidate: string;
};

export type ResearchPortfolio = {
  states: string[];
  summary: Record<string, unknown>;
  metric_definitions: Record<string, MetricDefinition>;
  timeline: PortfolioTimelineEvent[];
  comparison: CandidateComparisonRow[];
  clusters: EvidenceCluster[];
  candidates: LifecycleCandidate[];
};

export type PaperAccount = {
  id: number;
  name: string;
  base_currency: string;
  starting_cash: string | number;
  cash_balance: string | number;
  realized_pnl: string | number;
  status: string;
  simulation_only: boolean;
  created_at?: string;
};

export type PaperBalance = PaperAccount & {
  market_value: string | number;
  unrealized_pnl: string | number;
  equity: string | number;
};

export type PaperOrder = {
  id: number;
  account_id: number;
  deployment_id?: number | null;
  symbol: string;
  timeframe: string;
  side: string;
  order_type: string;
  quantity: string | number;
  limit_price?: string | number | null;
  trigger_price?: string | number | null;
  parent_order_id?: number | null;
  stop_loss_price?: string | number | null;
  take_profit_price?: string | number | null;
  status: string;
  submitted_at?: string;
  filled_at?: string | null;
  rejected_reason?: string | null;
  simulation_only: boolean;
};

export type ExecutionLog = {
  id: number;
  account_id: number;
  deployment_id?: number | null;
  order_id?: number | null;
  event_type: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type PaperFill = {
  id: number;
  order_id: number;
  account_id: number;
  symbol: string;
  side: string;
  quantity: string | number;
  fill_price: string | number;
  gross_amount: string | number;
  fee: string | number;
  filled_at: string;
  simulation_only: boolean;
};

export type PaperPosition = {
  account_id: number;
  symbol: string;
  quantity: string | number;
  average_price: string | number;
  realized_pnl: string | number;
  last_price?: string | number;
  market_value?: string | number;
  unrealized_pnl?: string | number;
};

export type PaperEquityPoint = {
  id: number;
  account_id: number;
  timestamp: string;
  cash_balance: string | number;
  equity: string | number;
  unrealized_pnl: string | number;
  realized_pnl: string | number;
};

export type StrategyDeployment = {
  id: number;
  account_id: number;
  strategy_name: string;
  strategy_version: string;
  symbol: string;
  timeframe: string;
  parameters: Record<string, unknown>;
  status: string;
  simulation_only: boolean;
  created_at?: string;
  paused_at?: string | null;
  resumed_at?: string | null;
  scan_cadence?: "scheduler" | "manual" | "15m" | "30m" | "60m" | "daily";
  max_simulated_exposure_pct?: string | number;
  health_status?: string;
  health_checked_at?: string | null;
  last_scan_at?: string | null;
  last_signal?: string | null;
  last_check_result?: string | null;
  last_scan_payload?: Record<string, unknown>;
  last_scanned_candle_timestamp?: string | null;
};

export type PaperSchedulerStatus = {
  id: boolean;
  enabled: boolean;
  cadence: "manual" | "15m" | "30m" | "60m";
  last_run_at?: string | null;
  next_run_at?: string | null;
  latest_result?: string | null;
  latest_error?: string | null;
  is_running: boolean;
  running_since?: string | null;
  updated_at?: string;
};

export type EvidenceAlert = {
  id: number;
  symbol: string;
  timeframe: string;
  strategy_id: string;
  alert_type: "entry_setup_review" | "exit_risk_review" | "avoid_condition" | "stale_data_warning" | "scheduler_error" | "duplicate_candle_skip";
  severity: "info" | "warning" | "critical";
  verdict: string;
  evidence_summary: string;
  matched_rules: string[];
  failed_rules: string[];
  profit_factor?: string | number | null;
  expectancy?: string | number | null;
  trade_count?: number | null;
  max_drawdown?: string | number | null;
  regime?: string | null;
  candle_timestamp?: string | null;
  created_at: string;
  acknowledged_at?: string | null;
  simulation_only: boolean;
};

export type SignalReview = {
  id: number;
  account_id?: number | null;
  deployment_id?: number | null;
  symbol: string;
  timeframe: string;
  strategy_id: string;
  status: "No Setup" | "Setup Forming" | "Setup Worth Reviewing" | "In Paper Position" | "Exit Risk Worth Reviewing" | "Invalidated" | "Stale Data Blocked";
  verdict: "No Setup" | "Setup Worth Reviewing" | "Exit Risk Worth Reviewing" | "Stale Data Blocked" | "Invalidated";
  regime?: string | null;
  evidence_score: string;
  matched_rules: string[];
  failed_rules: string[];
  profit_factor?: string | number | null;
  expectancy?: string | number | null;
  trade_count?: number | null;
  max_drawdown?: string | number | null;
  latest_candle_timestamp?: string | null;
  data_freshness: string;
  possible_entry_price?: string | number | null;
  invalidation_level?: string | number | null;
  risk_target?: string | number | null;
  exit_zone?: string | number | null;
  risk_per_share?: string | number | null;
  reward_per_share?: string | number | null;
  risk_reward_ratio?: string | number | null;
  max_holding_bars?: number | null;
  note?: string | null;
  reviewed_at?: string | null;
  ignored_at?: string | null;
  sent_to_paper_simulation_at?: string | null;
  created_at: string;
  updated_at?: string;
  disclaimer: string;
  simulation_only: boolean;
};

export type PaperScanResult = {
  deployment: StrategyDeployment;
  action: string;
  message: string;
  decision: Record<string, unknown>;
  sync: Record<string, unknown>;
  features: Record<string, unknown>;
  processed_pending: Record<string, unknown>;
  order?: PaperOrder | null;
  position: PaperPosition;
  reconciliation: Record<string, unknown>;
  simulation_only: boolean;
};

export type MissionControlStatus = "Healthy" | "Warning" | "Stale" | "Error" | "Disabled";

export type MissionControlAsset = {
  symbol: string;
  asset_class: string;
  timeframe: string;
  selected_strategy: string;
  deployment_status: string;
  status: string;
  latest_verdict: string;
  evidence_score: string;
  profit_factor?: string | number | null;
  expectancy?: string | number | null;
  trade_count?: number | null;
  max_drawdown?: string | number | null;
  current_regime?: string | null;
  latest_candle_timestamp?: string | null;
  data_age_hours?: number | null;
  data_freshness: MissionControlStatus;
  data_freshness_detail: string;
  latest_scan_timestamp?: string | null;
  alert_severity?: string | null;
  paper_position_status: string;
  simulated_unrealized_pnl?: string | number | null;
  links: Record<string, string>;
};

export type MissionControlQueueItem = {
  symbol: string;
  reason: string;
  severity: string;
  timestamp?: string | null;
  strategy: string;
  current_verdict: string;
  priority: number;
  action: { label: string; href: string };
};

export type MissionControlDeployment = {
  id: number;
  asset: string;
  timeframe: string;
  strategy: string;
  candidate_identifier: string;
  deployment_state: string;
  last_scanned_candle?: string | null;
  last_decision?: string | null;
  last_successful_scan?: string | null;
  latest_alert?: EvidenceAlert | null;
  paper_position?: PaperPosition | null;
  simulated_unrealized_pnl?: string | number | null;
  links: Record<string, string>;
};

export type MissionControlActivity = {
  event_type: string;
  symbol?: string | null;
  description: string;
  timestamp?: string | null;
  status: string;
  link?: string | null;
};

export type MissionControlSnapshot = {
  generated_at: string;
  simulation_only: boolean;
  safety: {
    status: string;
    detail: string;
    simulation_only: boolean;
    live_routing_enabled: boolean;
    broker_order_routing: string;
  };
  system_health: {
    overall_status: MissionControlStatus;
    research_engine_status: MissionControlStatus;
    scheduler_status: MissionControlStatus;
    scheduler_cadence?: string | null;
    last_successful_scan?: string | null;
    last_successful_scheduler_run?: string | null;
    next_scheduled_scan?: string | null;
    latest_completed_candle?: string | null;
    overall_data_freshness: MissionControlStatus;
    active_deployment_count: number;
    unacknowledged_alert_count: number;
    simulation_safety_status: string;
    scheduler_failures: number;
    duplicate_candle_skips: number;
  };
  research_summary: Record<string, string | number | null>;
  assets: MissionControlAsset[];
  review_queue: MissionControlQueueItem[];
  deployments: MissionControlDeployment[];
  paper_account: {
    simulation_only: boolean;
    account_count: number;
    equity: string | number;
    cash: string | number;
    open_positions: number;
    realized_pnl: string | number;
    unrealized_pnl: string | number;
    recent_simulated_orders: PaperOrder[];
    recent_simulated_fills: PaperFill[];
    recent_equity_curve: PaperEquityPoint[];
    label: string;
  };
  recent_activity: MissionControlActivity[];
  daily_summary: Record<string, string | number | null>;
  subsystem_errors: Array<{ subsystem: string; error: string }>;
};

export type DailyResearchReport = {
  id: number;
  report_date: string;
  summary: {
    report_date: string;
    assets_scanned: { count: number; symbols: string[] };
    setups_found: { count: number; alerts: Array<Record<string, unknown>>; reviews: Array<Record<string, unknown>> };
    no_setup_decisions: { count: number; reviews: Array<Record<string, unknown>> };
    stale_data_blocks: { count: number; items: Array<Record<string, unknown>> };
    scheduler_errors: { count: number; items: Array<Record<string, unknown>> };
    paper_orders: { count: number; items: Array<Record<string, unknown>> };
    paper_fills: { count: number; items: Array<Record<string, unknown>> };
    pnl: { realized: string | number; unrealized: string | number; equity: string | number; label: string };
    data_freshness: { counts: Record<string, number>; assets: Array<Record<string, unknown>> };
    scheduler_uptime: string | number | null;
    important_alerts: { count: number; items: Array<Record<string, unknown>> };
    scan_activity: { count: number; items: Array<Record<string, unknown>> };
    simulation_only: boolean;
    safety: string;
  };
  markdown_report: string;
  generated_at: string;
  simulation_only: boolean;
};

export type DailyReportAnalytics = {
  simulation_only: boolean;
  generated_at: string;
  series: Array<{
    report_date: string;
    scheduler_uptime: number | null;
    stale_data_blocks: number;
    setups_found: number;
    no_setup_decisions: number;
    realized_pnl: number;
    unrealized_pnl: number;
    equity: number;
    scheduler_errors: number;
    paper_orders: number;
    paper_fills: number;
    important_alerts: number;
    fresh_assets: number;
    warning_assets: number;
    stale_assets: number;
  }>;
  windows: Record<string, Record<string, number | string | null>>;
  asset_comparison: Array<Record<string, string | number>>;
  strategy_comparison: Array<Record<string, string | number>>;
  recurring_operational_failures: Array<Record<string, unknown>>;
  weekly_summary: {
    window: string;
    summary: Record<string, number | string | null>;
    top_assets: Array<Record<string, string | number>>;
    top_strategies: Array<Record<string, string | number>>;
    recurring_failures: Array<Record<string, unknown>>;
    narrative: string;
    simulation_only: boolean;
  };
};

export type DeploymentConflict = {
  type: string;
  severity: "info" | "warning" | "critical" | string;
  deployment_id: number;
  symbol?: string;
  message: string;
  related_deployment_ids?: number[];
  exposure_pct?: string | number;
  limit_pct?: string | number;
};

export type ManagedDeployment = StrategyDeployment & {
  health_status: "Healthy" | "Warning" | "Error" | "Paused" | string;
  health_detail: string;
  position?: PaperPosition | null;
  exposure_pct: string | number;
  orders_count: number;
  fills_count: number;
  latest_alert?: EvidenceAlert | null;
  audit_events: ExecutionLog[];
  conflicts: DeploymentConflict[];
  performance: {
    realized_pnl: string | number;
    unrealized_pnl: string | number;
    market_value: string | number;
    exposure_pct: string | number;
    orders: number;
    fills: number;
    last_signal?: string | null;
    last_scan_at?: string | null;
  };
};

export type DeploymentComparisonRow = {
  name: string;
  deployment_count: number;
  active_count: number;
  paused_count: number;
  healthy_count: number;
  warning_count: number;
  error_count: number;
  orders: number;
  fills: number;
  realized_pnl: string | number;
  unrealized_pnl: string | number;
};

export type DeploymentManagementSnapshot = {
  generated_at: string;
  simulation_only: boolean;
  safety: string;
  summary: Record<string, string | number>;
  portfolio_risk: {
    cash: string | number;
    equity: string | number;
    market_value: string | number;
    realized_pnl: string | number;
    unrealized_pnl: string | number;
    gross_exposure_pct: string | number;
    open_positions: number;
    active_deployments: number;
    conflict_count: number;
    exposure_limit_breaches: number;
    top_positions: PaperPosition[];
    simulation_only: boolean;
  };
  deployments: ManagedDeployment[];
  conflicts: DeploymentConflict[];
  asset_comparison: DeploymentComparisonRow[];
  strategy_comparison: DeploymentComparisonRow[];
  audit_history: ExecutionLog[];
};

export type ResearchAssetInput = {
  symbol: string;
  timeframe?: string;
  provider?: string;
  limit?: number;
};

export type StrategyResearchInput = ResearchAssetInput & {
  strategy?: string;
};

export type AlphaDiscoveryInput = ResearchAssetInput & {
  maxCandidates?: number;
  monteCarloRuns?: number;
};

export type AlphaValidationInput = {
  symbols?: string[];
  timeframes?: string[];
  maxCandidates?: number;
  monteCarloRuns?: number;
  bootstrapRuns?: number;
};

type ApiRequestInit = RequestInit & {
  timeoutMs?: number;
};

async function request<T>(path: string, options?: ApiRequestInit): Promise<T> {
  const controller = new AbortController();
  const { timeoutMs = 3500, ...fetchOptions } = options ?? {};
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const response = await fetch(`${API_URL}${path}`, {
    ...fetchOptions,
    cache: "no-store",
    signal: controller.signal,
    headers: {
      "Content-Type": "application/json",
      ...(fetchOptions.headers ?? {})
    }
  }).finally(() => clearTimeout(timeout));
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export function getCandles(limit = 220, input: ResearchAssetInput = { symbol: "BTCUSDT", timeframe: "4h" }) {
  const params = new URLSearchParams({
    timeframe: input.timeframe ?? "4h",
    limit: String(limit)
  });
  return request<Candle[]>(`/candles/${encodeURIComponent(input.symbol)}?${params.toString()}`);
}

export function getSignal() {
  return request<Signal>("/signals/BTCUSDT?timeframe=4h");
}

export function generateSignal() {
  return request<Signal>("/signals/generate?symbol=BTCUSDT&timeframe=4h", { method: "POST" });
}

export function syncCandles(input: ResearchAssetInput = { symbol: "BTCUSDT" }) {
  const params = new URLSearchParams({
    symbol: input.symbol,
    timeframe: input.timeframe ?? "4h",
    provider: input.provider ?? "binance_dev",
    limit: String(input.limit ?? 1500)
  });
  return request<Record<string, unknown>>(`/data/sync?${params.toString()}`, { method: "POST", timeoutMs: 120000 });
}

export function syncFeatures(input: ResearchAssetInput = { symbol: "BTCUSDT" }) {
  const params = new URLSearchParams({
    symbol: input.symbol,
    timeframe: input.timeframe ?? "4h"
  });
  return request<Record<string, unknown>>(`/features/sync?${params.toString()}`, { method: "POST", timeoutMs: 120000 });
}

export function runBacktest() {
  return request<BacktestResult>("/backtests?symbol=BTCUSDT&timeframe=4h", { method: "POST", timeoutMs: 120000 });
}

export function runStrategyResearch(input: StrategyResearchInput = { symbol: "BTCUSDT" }) {
  const params = new URLSearchParams({
    symbol: input.symbol,
    timeframe: input.timeframe ?? "4h"
  });
  if (input.strategy) params.set("strategy", input.strategy);
  return request<StrategyResearchReport>(`/research/strategies?${params.toString()}`, { method: "POST", timeoutMs: 120000 });
}

export function runAlphaDiscovery(input: number | AlphaDiscoveryInput = 250) {
  const normalized: AlphaDiscoveryInput = typeof input === "number" ? { symbol: "BTCUSDT", maxCandidates: input } : input;
  const params = new URLSearchParams({
    symbol: normalized.symbol,
    timeframe: normalized.timeframe ?? "4h",
    max_candidates: String(normalized.maxCandidates ?? 250),
    monte_carlo_runs: String(normalized.monteCarloRuns ?? 50)
  });
  return request<AlphaDiscoveryReport>(`/alpha/discover?${params.toString()}`, { method: "POST", timeoutMs: 120000 });
}

export function runAlphaValidation(input: number | AlphaValidationInput = 50) {
  const normalized: AlphaValidationInput = typeof input === "number" ? { maxCandidates: input } : input;
  const params = new URLSearchParams();
  params.set("max_candidates", String(normalized.maxCandidates ?? 50));
  params.set("monte_carlo_runs", String(normalized.monteCarloRuns ?? 50));
  params.set("bootstrap_runs", String(normalized.bootstrapRuns ?? 50));
  for (const symbol of normalized.symbols ?? ["BTCUSDT", "ETHUSDT"]) params.append("symbols", symbol);
  for (const timeframe of normalized.timeframes ?? ["4h", "1d"]) params.append("timeframes", timeframe);
  return request<AlphaValidationReport>(`/alpha/validate?${params.toString()}`, { method: "POST", timeoutMs: 180000 });
}

export function getRiskSettings() {
  return request<RiskSettings>("/risk/settings");
}

export function updateRiskSettings(payload: Partial<RiskSettings>) {
  return request<RiskSettings>("/risk/settings", { method: "PUT", body: JSON.stringify(payload) });
}

export function askCopilot(question: string) {
  return request<CopilotResponse>("/research/copilot", { method: "POST", body: JSON.stringify({ question }), timeoutMs: 60000 });
}

export function getCopilotInteractions() {
  return request<CopilotInteraction[]>("/research/copilot/interactions");
}

export function getSymbols() {
  return request<SymbolRow[]>("/symbols");
}

export function getResearchHypotheses() {
  return request<ResearchHypothesis[]>("/research/hypotheses");
}

export function createResearchHypothesis(payload: HypothesisPayload) {
  return request<ResearchHypothesis>("/research/hypotheses", { method: "POST", body: JSON.stringify(payload) });
}

export function runHypothesisExperiment(
  hypothesisId: number,
  options?: { maxCandidates?: number; monteCarloRuns?: number; bootstrapRuns?: number; symbols?: string[]; timeframes?: string[] }
) {
  const params = new URLSearchParams();
  params.set("max_candidates", String(options?.maxCandidates ?? 5));
  params.set("monte_carlo_runs", String(options?.monteCarloRuns ?? 10));
  params.set("bootstrap_runs", String(options?.bootstrapRuns ?? 10));
  for (const symbol of options?.symbols ?? ["BTCUSDT"]) params.append("symbols", symbol);
  for (const timeframe of options?.timeframes ?? ["4h"]) params.append("timeframes", timeframe);
  return request<Record<string, unknown>>(`/research/hypotheses/${hypothesisId}/experiments?${params.toString()}`, { method: "POST", timeoutMs: 180000 });
}

export function getResearchJournal() {
  return request<ResearchJournalEntry[]>("/research/journal");
}

export function getResearchTimeline() {
  return request<ResearchTimelineEvent[]>("/research/timeline");
}

export function getResearchArchive() {
  return request<ResearchArchiveRow[]>("/research/archive");
}

export function getResearchIntelligence() {
  return request<ResearchIntelligence>("/research/intelligence");
}

export function getValidationRuns() {
  return request<ValidationRun[]>("/alpha/validation-runs");
}

export function getValidationRun(runId: number | string) {
  return request<ValidationRunDetail>(`/alpha/validation-runs/${runId}`, { timeoutMs: 60000 });
}

export function getStrategyExperiments(options?: { strategy?: string }) {
  const params = new URLSearchParams();
  if (options?.strategy) params.set("strategy", options.strategy);
  const suffix = params.size ? `?${params.toString()}` : "";
  return request<StrategyExperimentDefinition[]>(`/research/strategy-experiments${suffix}`, { timeoutMs: 60000 });
}

export function getStrategyExperiment(experimentId: string) {
  return request<StrategyExperimentDefinition>(`/research/strategy-experiments/${encodeURIComponent(experimentId)}`, { timeoutMs: 60000 });
}

export function getPromisingResearchCandidates(options?: { maxCandidates?: number; maxRunsPerExperiment?: number; foldCount?: number }) {
  const params = new URLSearchParams();
  params.set("max_candidates", String(options?.maxCandidates ?? 24));
  params.set("max_runs_per_experiment", String(options?.maxRunsPerExperiment ?? 6));
  params.set("fold_count", String(options?.foldCount ?? 2));
  return request<PromisingResearchReport>(`/research/promising-candidates?${params.toString()}`, { timeoutMs: 240000 });
}

export function getResearchPortfolio(options?: { maxCandidates?: number }) {
  const params = new URLSearchParams();
  params.set("max_candidates", String(options?.maxCandidates ?? 24));
  return request<ResearchPortfolio>(`/research/portfolio?${params.toString()}`, { timeoutMs: 240000 });
}

export function getPaperAccounts() {
  return request<PaperAccount[]>("/paper/accounts");
}

export function createPaperAccount(payload: { name: string; starting_cash: number; base_currency?: string }) {
  return request<PaperAccount>("/paper/accounts", { method: "POST", body: JSON.stringify(payload) });
}

export function getPaperBalances(accountId: number) {
  return request<PaperBalance>(`/paper/accounts/${accountId}/balances`);
}

export function getPaperPositions(accountId: number) {
  return request<PaperPosition[]>(`/paper/accounts/${accountId}/positions`);
}

export function getPaperOrders(accountId: number) {
  return request<PaperOrder[]>(`/paper/accounts/${accountId}/orders`);
}

export function getPaperFills(accountId: number) {
  return request<PaperFill[]>(`/paper/accounts/${accountId}/fills`);
}

export function getPaperEquityCurve(accountId: number) {
  return request<PaperEquityPoint[]>(`/paper/accounts/${accountId}/equity-curve`);
}

export function createPaperOrder(payload: { account_id: number; symbol: string; quantity: number; side?: string; order_type?: string; timeframe?: string; limit_price?: number; deployment_id?: number; stop_loss_price?: number; take_profit_price?: number }) {
  return request<PaperOrder>("/paper/orders", { method: "POST", body: JSON.stringify(payload) });
}

export function cancelPaperOrder(orderId: number) {
  return request<PaperOrder>(`/paper/orders/${orderId}/cancel`, { method: "POST" });
}

export function processPendingPaperOrders(accountId: number) {
  return request<{ processed: number; filled: number; pending: number }>(`/paper/orders/process?account_id=${accountId}`, { method: "POST" });
}

export function getExecutionLogs(accountId: number) {
  return request<ExecutionLog[]>(`/paper/accounts/${accountId}/execution-logs`);
}

export function reconcilePaperAccount(accountId: number, repair = false) {
  return request<{ healthy: boolean; repaired: boolean; issue_count: number; issues: unknown[] }>(`/paper/accounts/${accountId}/reconcile`, { method: "POST", body: JSON.stringify({ repair }) });
}

export function getStrategyDeployments(accountId?: number) {
  const params = new URLSearchParams();
  if (accountId) params.set("account_id", String(accountId));
  return request<StrategyDeployment[]>(`/paper/deployments${params.size ? `?${params.toString()}` : ""}`);
}

export function createStrategyDeployment(payload: { account_id: number; strategy_name: string; symbol: string; timeframe?: string; strategy_version?: string; parameters?: Record<string, unknown> }) {
  return request<StrategyDeployment>("/paper/deployments", { method: "POST", body: JSON.stringify(payload) });
}

export function deployTslaMomentumBull(accountId: number) {
  return request<StrategyDeployment>(`/paper/deployments/tsla-momentum-bull?account_id=${accountId}`, { method: "POST" });
}

export function scanStrategyDeployment(deploymentId: number) {
  return request<PaperScanResult>(`/paper/deployments/${deploymentId}/scan`, { method: "POST", timeoutMs: 180000 });
}

export function getPaperScheduler() {
  return request<PaperSchedulerStatus>("/paper/scheduler");
}

export function getEvidenceAlerts(options?: { limit?: number; includeAcknowledged?: boolean }) {
  const params = new URLSearchParams();
  params.set("limit", String(options?.limit ?? 100));
  params.set("include_acknowledged", String(options?.includeAcknowledged ?? true));
  return request<EvidenceAlert[]>(`/paper/alerts?${params.toString()}`);
}

export function getSignalReviews(options?: { accountId?: number; limit?: number }) {
  const params = new URLSearchParams();
  if (options?.accountId) params.set("account_id", String(options.accountId));
  params.set("limit", String(options?.limit ?? 25));
  return request<SignalReview[]>(`/paper/signal-reviews?${params.toString()}`);
}

export function generateSignalReview(deploymentId: number) {
  return request<SignalReview>(`/paper/deployments/${deploymentId}/signal-review`, { method: "POST", timeoutMs: 180000 });
}

export function markSignalReviewReviewed(reviewId: number) {
  return request<SignalReview>(`/paper/signal-reviews/${reviewId}/mark-reviewed`, { method: "POST" });
}

export function ignoreSignalReview(reviewId: number) {
  return request<SignalReview>(`/paper/signal-reviews/${reviewId}/ignore`, { method: "POST" });
}

export function sendSignalReviewToPaperSimulation(reviewId: number) {
  return request<SignalReview>(`/paper/signal-reviews/${reviewId}/send-to-paper-simulation`, { method: "POST" });
}

export function addSignalReviewNote(reviewId: number, note: string) {
  return request<SignalReview>(`/paper/signal-reviews/${reviewId}/note`, { method: "POST", body: JSON.stringify({ note }) });
}

export function acknowledgeEvidenceAlert(alertId: number) {
  return request<EvidenceAlert>(`/paper/alerts/${alertId}/acknowledge`, { method: "POST" });
}

export function updatePaperScheduler(payload: { enabled?: boolean; cadence?: "manual" | "15m" | "30m" | "60m" }) {
  return request<PaperSchedulerStatus>("/paper/scheduler", { method: "PUT", body: JSON.stringify(payload) });
}

export function runPaperSchedulerNow() {
  return request<Record<string, unknown>>("/paper/scheduler/run", { method: "POST", timeoutMs: 180000 });
}

export function pauseStrategyDeployment(deploymentId: number) {
  return request<StrategyDeployment>(`/paper/deployments/${deploymentId}/pause`, { method: "POST" });
}

export function resumeStrategyDeployment(deploymentId: number) {
  return request<StrategyDeployment>(`/paper/deployments/${deploymentId}/resume`, { method: "POST" });
}

export function updateDeploymentControls(deploymentId: number, payload: { scan_cadence?: string; max_simulated_exposure_pct?: number }) {
  return request<StrategyDeployment>(`/paper/deployments/${deploymentId}/controls`, { method: "PUT", body: JSON.stringify(payload) });
}

export function bulkPauseDeployments(deploymentIds?: number[]) {
  return request<Record<string, unknown>>("/paper/deployments/bulk-pause", { method: "POST", body: JSON.stringify({ deployment_ids: deploymentIds }) });
}

export function bulkScanDeployments(deploymentIds?: number[]) {
  return request<Record<string, unknown>>("/paper/deployments/bulk-scan", { method: "POST", body: JSON.stringify({ deployment_ids: deploymentIds }), timeoutMs: 180000 });
}

export function getMissionControl() {
  return request<MissionControlSnapshot>("/paper/mission-control", { timeoutMs: 60000 });
}

export function getDeploymentManagement() {
  return request<DeploymentManagementSnapshot>("/paper/deployment-management", { timeoutMs: 60000 });
}

export function getDailyResearchReports(limit = 30) {
  return request<DailyResearchReport[]>(`/paper/daily-reports?limit=${limit}`, { timeoutMs: 60000 });
}

export function generateDailyResearchReport(reportDate?: string) {
  const suffix = reportDate ? `?report_date=${encodeURIComponent(reportDate)}` : "";
  return request<DailyResearchReport>(`/paper/daily-reports${suffix}`, { method: "POST", timeoutMs: 60000 });
}

export function getDailyReportAnalytics() {
  return request<DailyReportAnalytics>("/paper/daily-reports/analytics", { timeoutMs: 60000 });
}
