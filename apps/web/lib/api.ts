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

export type ResearchCommandCenterFilters = {
  campaignId?: number;
  asset?: string;
  assetClass?: string;
  timeframe?: string;
  strategyFamily?: string;
  candidateState?: string;
  validationRule?: string;
  regime?: string;
  dateFrom?: string;
  dateTo?: string;
};

export type ResearchCommandCenter = {
  live_evidence?: boolean;
  campaign: Record<string, any> | null;
  campaigns: Array<Record<string, any>>;
  filters: Record<string, string>;
  filter_options: Record<string, string[]>;
  overview: Record<string, number>;
  candidate_funnel: Array<Record<string, any>>;
  rejection_analysis: Record<string, any>;
  near_pass_candidates: Array<Record<string, any>>;
  strategy_intelligence: { rows: Array<Record<string, any>>; highlights: Record<string, string | null> };
  asset_intelligence: { rows: Array<Record<string, any>>; highlights: Record<string, string | null> };
  timeframe_intelligence: { rows: Array<Record<string, any>>; highlights: Record<string, string | null> };
  regime_analysis: Array<Record<string, any>>;
  duplicate_analysis: Record<string, any>;
  experiment_history: Array<Record<string, any>>;
  recommendations: Array<Record<string, any>>;
  next_campaign_proposal: Record<string, any> | null;
  historical_research: Record<string, any>;
  terminology: Record<string, string>;
  reconciliation: Record<string, any>;
  source: Record<string, any>;
  simulation_only: boolean;
};

export type PersistedCandidateProfile = {
  candidate_id: string;
  campaign_ids: number[];
  state: string;
  deployment_status: string;
  asset: string;
  assets: string[];
  timeframe: string;
  timeframes: string[];
  strategy_family: string;
  generation_method?: string;
  parent_candidate?: string | null;
  complete_parameter_set: Record<string, unknown>;
  research_score?: number | string | null;
  profit_factor?: number | string | null;
  expectancy?: number | string | null;
  trade_count?: number | string | null;
  maximum_drawdown?: number | string | null;
  stability?: number | string | null;
  strategy_definition: Record<string, unknown>;
  research_metrics: Record<string, unknown>;
  validation_gates: Array<Record<string, unknown>>;
  walk_forward_evidence: Array<Record<string, unknown>>;
  out_of_sample_evidence: Array<Record<string, unknown>>;
  regime_evidence: Array<Record<string, unknown>>;
  cross_asset_evidence: Array<Record<string, unknown>>;
  campaign_lineage: Array<Record<string, unknown>>;
  repair_history: Array<Record<string, unknown>>;
  paper_deployment_status: Array<Record<string, unknown>>;
  forward_performance: Record<string, unknown>;
  backtest_versus_forward: Record<string, unknown>;
  readiness_blockers: string[];
  evidence_plan: { missing_evidence_reason: string; status: string; steps: Array<Record<string, unknown>> };
  diagnostic_report: string;
  technical_details: Record<string, unknown>;
  simulation_only: boolean;
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

export type StrategyDiscoveryRow = {
  candidate_id: string;
  family_id: string;
  parent_candidate_id?: string | null;
  symbol: string;
  timeframe: string;
  generation: number;
  blocks: Record<string, string>;
  metrics: Record<string, unknown>;
  research_score: number;
  status: "generated" | "rejected" | "promoted" | "retired";
  failure_reasons: string[];
  explanation: string;
  created_at?: string;
};

export type StrategyDiscoveryDashboard = {
  summary: {
    generated: number;
    rejected: number;
    promoted: number;
    retired: number;
    families: number;
  };
  strongest_discoveries: StrategyDiscoveryRow[];
  newest_discoveries: StrategyDiscoveryRow[];
  evolution_history: Array<Record<string, unknown>>;
  successful_rule_combinations: Array<{ combination: string; count: number; best_score: number | null }>;
  safety: string;
};

export type StrategyDiscoveryRun = {
  run_id: number;
  symbol: string;
  timeframe: string;
  generated: number;
  evaluated: number;
  promoted: number;
  rejected: number;
  leaderboard: StrategyDiscoveryRow[];
  safety: string;
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
  ready_1h_candles?: number;
  ready_4h_candles?: number;
  latest_1h_candle_timestamp?: string | null;
  latest_4h_candle_timestamp?: string | null;
  ready_1h_features?: number;
  ready_4h_features?: number;
  research_ready?: boolean;
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
  candidate_id?: string | null;
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
  deployment_id?: number | null;
  candidate_id?: string | null;
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
  campaign_id?: number | null;
  candidate_id?: string | null;
  strategy_id?: string | null;
  forward_validation_started_at?: string | null;
  evidence_version?: string | null;
  lifecycle_state?: string | null;
  deployment_origin?: string | null;
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
  alert_type: "entry_setup_review" | "exit_risk_review" | "avoid_condition" | "stale_data_warning" | "scheduler_error" | "duplicate_candle_skip" | "evidence_drift_warning";
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
  snapshot_version?: string;
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
  health?: Record<string, any>;
  readiness?: {
    state: string;
    score: string | number | null;
    phase_10_allowed: boolean;
    blocking_gate_count: number;
    blocking_gates: Array<Record<string, any>>;
    passed_gates: Array<Record<string, any>>;
    gates: Array<Record<string, any>>;
    last_assessed_at?: string | null;
    snapshot_source?: string;
  };
  campaign?: Record<string, any>;
  workers?: Record<string, any>;
  market_data?: Record<string, any>;
  forward_evidence?: Record<string, any>;
  diagnostics?: {
    active: Array<Record<string, any>>;
    resolved: Array<Record<string, any>>;
    history: Array<Record<string, any>>;
    active_count: number;
  };
  invariants?: Array<Record<string, any>>;
  research_summary: Record<string, string | number | null>;
  asset_count?: number;
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
  research_campaigns?: Record<string, any>;
  research_learning?: Record<string, any>;
  production_validation?: Record<string, any>;
  external_broker_paper?: {
    provider: string;
    environment: string;
    execution_enabled: boolean;
    order_submission_implemented: boolean;
    feature_flags: Record<string, boolean>;
    account?: Record<string, any>;
    latest_sync?: Record<string, any>;
    latest_reconciliation?: Record<string, any>;
    adapter?: Record<string, any>;
    active_halts: Array<Record<string, any>>;
    deployments: Array<Record<string, any>>;
    epochs: Array<Record<string, any>>;
    shadow_executions: Array<Record<string, any>>;
    daily_summary?: Record<string, any>;
    opportunity_coverage?: {
      classification: string;
      active_elites: number;
      unique_symbols: number;
      unique_timeframes: number;
      dominant_symbol?: string | null;
      dominant_symbol_share: number;
      symbol_distribution: Record<string, number>;
      timeframe_distribution: Record<string, number>;
      setup_frequency_today: number;
      long_only: boolean;
      external_short_execution_enabled: boolean;
      research_recommendations: Array<{ code: string; status: string; detail: string }>;
    };
    elite_activity?: Array<{
      id: number;
      candidate_id: string;
      symbol: string;
      timeframe: string;
      state: string;
      research_score?: string | number | null;
      evaluations_today: number;
      setups_today: number;
      avoids_today: number;
      shadow_decisions_today: number;
      would_submit_today: number;
      latest_signal?: string | null;
      latest_bar?: string | null;
      latest_evaluation_at?: string | null;
      latest_gates?: Array<Record<string, any>>;
      latest_would_submit?: boolean | null;
      latest_rejection_reasons?: string[];
      latest_shadow_at?: string | null;
      execution_attempts_today: number;
      submitted_attempts_today: number;
      today_performance: {
        realized_pnl: number | null;
        unrealized_pnl: number | null;
        submitted_orders: number;
        attribution_status: string;
        simulation_only: boolean;
      };
      historical_replay: Record<string, any>;
    }>;
    generated_at: string;
  };
  recent_activity: MissionControlActivity[];
  daily_summary: Record<string, string | number | null>;
  subsystem_errors: Array<Record<string, any> & { subsystem: string; error: string }>;
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
  accounts?: PaperAccount[];
  positions?: PaperPosition[];
  orders?: PaperOrder[];
  fills?: PaperFill[];
  alerts?: EvidenceAlert[];
  logs?: ExecutionLog[];
  account_snapshots?: Array<{
    account: PaperAccount;
    balances: PaperBalance;
    positions: PaperPosition[];
    orders: PaperOrder[];
    fills: PaperFill[];
    equity: PaperEquityPoint[];
    logs: ExecutionLog[];
  }>;
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
  revalidateSeconds?: number;
};

export type ResearchUniverseInput = {
  universe_key: string;
  name: string;
  description: string;
  assets: string[];
  default_timeframes: string[];
  metadata: Record<string, unknown>;
};

export type ResearchLearningSummary = {
  global_learning?: {
    available?: boolean;
    snapshot_key?: string;
    reason?: string;
    decision_intelligence?: {
      assets?: Array<Record<string, unknown>>;
      strategy_families?: Array<Record<string, unknown>>;
      timeframes?: Array<Record<string, unknown>>;
    };
    campaign_guidance?: {
      search_prioritization?: {
        assets?: string[];
        strategy_families?: string[];
        timeframes?: string[];
      };
      campaign_budgeting?: Record<string, unknown>;
    };
    calculation_version?: string;
    created_at?: string;
  };
  safety?: Record<string, unknown>;
};

export type AlpacaStockAsset = {
  id: string;
  symbol: string;
  name: string;
  exchange: string;
  asset_class: "us_equity";
  status: "active";
  tradable: true;
  marginable: boolean;
  shortable: boolean;
  fractionable: boolean;
};

export type AlpacaAssetCatalog = {
  assets: AlpacaStockAsset[];
  total: number;
  imported: number;
  source: "alpaca";
};

export type ResearchCampaignRecord = {
  id: number;
  name: string;
  status: string;
  requested_candidates?: number;
  queued_jobs?: number;
  completed_jobs?: number;
  failed_jobs?: number;
  promoted_candidates?: number;
  rejected_candidates?: number;
  [key: string]: unknown;
};

export type ResearchCampaignAnalytics = {
  jobs_total?: number;
  completion_percentage?: number;
  jobs_by_status?: Record<string, number>;
  promoted?: number;
  rejected?: number;
  [key: string]: unknown;
};

export type ResearchCampaignCreateResult = {
  campaign: ResearchCampaignRecord;
  assets?: string[];
  timeframes?: string[];
  dataset?: Record<string, unknown>;
  hypothesis?: Record<string, unknown>;
  targeting?: Record<string, unknown>;
  candidate_generation?: Record<string, unknown>;
  candidates_generated: number;
  jobs_created: number;
  campaign_version: string;
  simulation_only: boolean;
  safety: string;
};

export type ResearchCampaignStatus = {
  campaign: ResearchCampaignRecord;
  analytics: ResearchCampaignAnalytics;
  recent_jobs: Array<Record<string, unknown>>;
  elite_candidates: Array<Record<string, unknown>>;
  forward_validation_candidates?: Array<Record<string, unknown>>;
  simulation_only: boolean;
};

export type ResearchCampaignBatchResult = {
  campaign_id: number;
  processed: number;
  completed: number;
  failed: number;
  remaining: number;
  analytics: ResearchCampaignAnalytics;
  simulation_only: boolean;
};

export type ResearchCampaignPreflight = {
  ready: boolean;
  can_launch?: boolean;
  assets_total: number;
  executable_assets?: string[];
  executable_assets_total?: number;
  excluded_assets?: string[];
  excluded_assets_total?: number;
  timeframes: string[];
  datasets_total: number;
  eligible_datasets: number;
  blocked_datasets: number;
  classifications: Record<string, number>;
  issues: Array<{
    symbol: string;
    timeframe: string;
    classification: string;
    reason: string;
    candle_count: number;
    feature_count: number;
    provider?: string | null;
  }>;
  issues_truncated: boolean;
  simulation_only: boolean;
};

export type ResearchCampaignPreparation = {
  ready: boolean;
  initial_readiness?: ResearchCampaignPreflight;
  datasets_considered?: number;
  prepared: Array<{ symbol: string; timeframe: string; provider: string; candles: number; features: number }>;
  errors: Array<{ symbol: string; timeframe: string; reason: string }>;
  readiness: ResearchCampaignPreflight;
  simulation_only: boolean;
};

export type ResearchArchitectureState = {
  architecture_version: string;
  active_dataset_id?: number | null;
  datasets: Array<Record<string, unknown>>;
  asset_profiles: Array<Record<string, unknown>>;
  clusters: Array<Record<string, unknown>>;
  hypotheses: Array<Record<string, unknown>>;
  cycles: Array<Record<string, unknown>>;
  archives: Array<Record<string, unknown>>;
  validation_policy: Record<string, unknown>;
  safety: Record<string, unknown>;
};

export type ResearchCampaignListRow = {
  id: number;
  name: string;
  universe_key: string;
  status: string;
  dataset_id?: number | null;
  dataset_mode?: "rolling" | "reproducibility" | null;
  generator_version?: string | null;
  requested_candidates: number;
  total_jobs: number;
  queued_jobs: number;
  running_jobs: number;
  blocked_jobs: number;
  terminal_blocked_jobs?: number;
  deferred_jobs: number;
  terminal_jobs: number;
  promoted_jobs: number;
  rejected_jobs: number;
  estimated_seconds_remaining?: number | null;
  eta_seconds?: number | null;
  eta_method?: string;
  sampled_terminal_jobs?: number;
  jobs_per_minute?: number;
  jobs_per_minute_5m?: number;
  jobs_per_minute_15m?: number;
  executable_remaining_jobs?: number;
  target_workers?: number;
  search_mode?: "scout_expand" | "full";
  research_stage?: "scout" | "expanded" | "stopped_no_signal" | "stopped_no_inventory" | "full";
  scout_candidate_count?: number;
  expanded_routes?: number;
  expansion_jobs_created?: number;
  average_queue_delay_ms?: number;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  updated_at: string;
};

export type ResearchCampaignList = {
  campaigns: ResearchCampaignListRow[];
  summary: {
    running: number;
    queued: number;
    paused: number;
  };
  simulation_only: boolean;
};

export type ResearchCampaignProfile = {
  campaign_id: number;
  profiled_jobs: number;
  average_ms: {
    total: number;
    loading_market_data: number;
    calculating_indicators: number;
    running_simulation: number;
    writing_results: number;
    database_queue_operations: number;
  };
  dataset_cache_hit_rate: number;
  runtime: {
    active_parallel_workers: number;
    active_parallel_jobs: number;
    configured_parallel_workers: number;
    target_workers: number;
    live_workers: number;
    draining_workers: number;
    effective_workers: number;
    starting_parallel_workers: number;
    parallel_pool_active: boolean;
    parallel_pool_status: "idle" | "starting" | "running";
    processed_parallel_jobs: number;
    preloaded_datasets: number;
    resident_memory_mb: number;
    worker_limit: number;
  };
  simulation_only: boolean;
};

export type StrategyDiagnosticsSummary = {
  evaluated: number;
  signals: Record<string, number>;
  setup_frequency: number;
  failed_gates: Array<{ code: string; count: number; rate: number }>;
  most_common_rejection: string | null;
  health: { label: string; score: number };
  sample_limited_to: number;
};

export type EliteDeploymentAudit = {
  items: Array<{
    elite_id: number;
    strategy_name: string;
    strategy_version: string;
    symbol?: string | null;
    timeframe?: string | null;
    internal_deployment_id?: number | null;
    internal_status?: string | null;
    external_deployment_id?: number | null;
    external_state?: string | null;
    blockers: string[];
  }>;
  counts: { elites: number; internal_deployments: number; external_deployments: number };
};

export type PortfolioReadiness = {
  positions: Array<Record<string, unknown>>;
  open_orders: Array<Record<string, unknown>>;
  recent_decisions: Array<Record<string, unknown>>;
  portfolio_heat_pct: string | number;
  heat_limit_pct: string | number;
  same_symbol_limit: number;
  correlation_limit: string | number;
};

export type ElitePortfolioConfiguration = {
  // Portfolio shape preset. Presets change size and spread only; quality
  // thresholds, correlation limits and the parameter-similarity rule are
  // identical in every one, and the API rejects a configuration that tries to
  // weaken them.
  profile?: string;
  universe: string[];
  families: string[];
  directions: string[];
  timeframes: string[];
  thresholds: Record<string, unknown>;
  constraints: Record<string, unknown>;
  objective: string;
  custom_size: number | null;
};

export type ElitePortfolioHardRule = { id: string; label: string; description: string };

export type ElitePortfolioOptions = {
  solver_version: string;
  universes: string[];
  families: string[];
  directions: string[];
  timeframes: string[];
  candidate_count: number;
  default_thresholds: Record<string, unknown>;
  default_constraints: Record<string, unknown>;
  hard_rules?: ElitePortfolioHardRule[];
  objectives: string[];
  maximum_portfolio_size: number;
  default_profile?: string;
  profiles?: ElitePortfolioProfile[];
  execution_policy: Record<string, string>;
};

export type ElitePortfolioProfile = {
  id: string;
  label: string;
  summary: string;
  intended_use: string;
  diversified: boolean;
  warning?: string;
  constraints: Record<string, number>;
  resolved_constraints: Record<string, number>;
};

export type ElitePortfolioBlocker = {
  setting: string;
  label: string;
  required: number | null;
  available: number | null;
  severity: "structural" | "eligibility" | "conflict";
  excluded?: number;
  detail: string;
};

export type ElitePortfolioBlockingAnalysis = {
  feasible: boolean;
  primary_blocker: ElitePortfolioBlocker | null;
  blockers: ElitePortfolioBlocker[];
  eligible_pool_size: number;
  pool_symbols: string[];
  pool_families: string[];
  pool_timeframes: string[];
};

export type ElitePortfolioProfileOutcome = {
  profile: string;
  label: string;
  summary?: string;
  diversified: boolean;
  warning?: string | null;
  feasible: boolean;
  size: number;
  eligible_count?: number;
  members?: string[];
  blocking?: ElitePortfolioBlocker | null;
  error?: string;
};

export type ElitePortfolioRecommendation = {
  recommended_profile: string | null;
  recommended_label?: string;
  recommended_size?: number;
  diversified?: boolean;
  warning?: string | null;
  reason: string;
  profiles: ElitePortfolioProfileOutcome[];
  constraints_relaxed_versus_strict: Array<{ setting: string; label: string; strict_value: unknown; profile_value: unknown }>;
  protected_constraints_unchanged: boolean;
};

export type ElitePortfolioVerification = {
  ran: boolean;
  verified: boolean;
  feasible: boolean | null;
  maximum_feasible_size: number | null;
  witness: string[] | null;
  pool_size: number;
  verification_limit: number | null;
  nodes_explored: number;
  duration_ms: number;
  termination_reason: string;
};

export type ElitePortfolioFeasibilityReport = {
  pool_size: number;
  total_possible_pairs: number;
  conflict_count_by_type: Record<string, number>;
  unique_conflict_edges: number;
  candidate_conflict_degree: Record<string, number>;
  available_symbols: string[];
  available_families: string[];
  available_timeframes: string[];
  symbol_count: number;
  family_count: number;
  timeframe_count: number;
  maximum_independent_set_size: number | null;
  maximum_independent_set_witness: string[] | null;
  maximum_independent_set_verified: boolean;
  maximum_feasible_size_after_all_constraints: number | null;
  minimum_unique_assets_independently_achievable: boolean;
  minimum_families_independently_achievable: boolean;
  exact_timeframe_balance_achievable: boolean | null;
  greedy_missed_a_valid_solution: boolean;
  verification_ran: boolean;
  verification_verified: boolean;
};

export type ElitePortfolioResult = {
  id?: number;
  status: string;
  solver_version?: string;
  selected?: string[];
  maximum_feasible_size?: number;
  eligible_count?: number;
  excluded_count?: number;
  constraint_relaxation_count?: number;
  binding_constraints?: Array<{ constraint: string; excluded_candidates_or_pairs: number }>;
  heuristic_miss?: boolean;
  verified_infeasible?: boolean;
  verification?: ElitePortfolioVerification;
  feasibility_report?: ElitePortfolioFeasibilityReport;
  blocking_analysis?: ElitePortfolioBlockingAnalysis | null;
  profile?: string | null;
  // Set only on an "All Validated Elites Paper Lab" result. `diversified` is
  // explicitly false there and the page must never present it as a
  // diversified portfolio merely because every member passed validation.
  mode?: string | null;
  diversified?: boolean;
  warning?: string;
  hard_rules?: ElitePortfolioHardRule[];
  analytics?: Record<string, any>;
  portfolio_analytics?: Record<string, any>;
  eligibility?: Array<Record<string, any>>;
  conflicts?: Array<Record<string, any>>;
  selection_explanations?: Array<Record<string, any>>;
  rejection_explanations?: Array<Record<string, any>>;
  snapshot?: { decision_hash?: string; snapshot_hash?: string; decision_inputs?: Record<string, any> } | null;
  snapshot_hash?: string;
  approved_snapshot_hash?: string | null;
  members?: Array<Record<string, any>>;
  statistics?: Record<string, any>;
  timing?: Record<string, number>;
  response_size_bytes?: number;
  cache?: { hit: boolean; key: string };
  execution_notice?: string;
  authorization_instructions?: Array<Record<string, any>>;
  errors?: Array<Record<string, any>>;
};

const API_TIMING_ENABLED = process.env.NODE_ENV !== "production" || process.env.NEXT_PUBLIC_DIAGNOSTIC_LOGGING === "true";

async function request<T>(path: string, options?: ApiRequestInit): Promise<T> {
  const controller = new AbortController();
  const { timeoutMs = 3500, revalidateSeconds, ...fetchOptions } = options ?? {};
  const startedAt = now();
  const method = fetchOptions.method ?? "GET";
  const resolvedCacheMode = fetchOptions.cache ?? cacheMode(path, method);
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let responseLogged = false;
  try {
    logFrontendDiagnostic("Fetch request sent", { method, path, timeoutMs });
    const response = await fetch(`${API_URL}${path}`, {
      ...fetchOptions,
      cache: resolvedCacheMode,
      ...(method === "GET" && resolvedCacheMode === "force-cache" ? { next: { revalidate: revalidateSeconds ?? 60 } } : {}),
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(method === "GET" && resolvedCacheMode === "no-store" ? { "Cache-Control": "no-cache", "Pragma": "no-cache" } : {}),
        ...(fetchOptions.headers ?? {})
      }
    });
    const requestId = response.headers.get("X-Request-ID");
    logApiTiming(path, method, startedAt, response.status);
    logFrontendDiagnostic("Fetch response received", { method, path, status: response.status, requestId, elapsedMs: Math.round(now() - startedAt) });
    responseLogged = true;
    if (!response.ok) {
      let detail = "";
      try {
        const payload = await response.json() as { detail?: unknown };
        detail = typeof payload.detail === "string" ? `: ${payload.detail}` : "";
      } catch {
        // Some upstream failures do not return JSON.
      }
      const error = new Error(`${response.status} ${response.statusText}${detail}`);
      logFrontendDiagnostic("Fetch failure", { method, path, status: response.status, requestId, errorClass: error.name, errorMessage: error.message, elapsedMs: Math.round(now() - startedAt) });
      throw error;
    }
    return response.json() as Promise<T>;
  } catch (error) {
    if (!responseLogged) logApiTiming(path, method, startedAt, "error", error);
    if (error instanceof Error && error.name === "AbortError") {
      logFrontendDiagnostic("Fetch abort", { method, path, timeoutMs, errorClass: error.name, errorMessage: error.message, elapsedMs: Math.round(now() - startedAt) });
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s.`);
    }
    logFrontendDiagnostic("Fetch unexpected exception", { method, path, errorClass: error instanceof Error ? error.name : typeof error, errorMessage: error instanceof Error ? error.message : String(error), elapsedMs: Math.round(now() - startedAt) });
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function cacheMode(path: string, method: string): RequestCache {
  if (method !== "GET") return "no-store";
  const critical = [
    "/broker/",
    "/portfolio/readiness",
    "/execution-attempts",
    "/reconciliation",
    "/approvals",
    "/halts",
    "/research/campaigns",
    "/research/campaign-workers",
    "/research/command-center",
    "/research/intraday/",
    "/research/elite-portfolios"
  ];
  return critical.some((token) => path.includes(token)) ? "no-store" : "force-cache";
}

function now() {
  return typeof performance === "undefined" ? Date.now() : performance.now();
}

function logApiTiming(path: string, method: string, startedAt: number, status: number | "error", error?: unknown) {
  if (!API_TIMING_ENABLED) return;
  const durationMs = Math.round(now() - startedAt);
  const prefix = `[api] ${method} ${path} -> ${status} in ${durationMs}ms`;
  if (status === "error") {
    console.warn(prefix, error instanceof Error ? error.message : error);
    return;
  }
  console.info(prefix);
}

function logFrontendDiagnostic(message: string, fields: Record<string, unknown>) {
  if (!API_TIMING_ENABLED) return;
  console.info(`[frontend-diagnostic] ${message}`, fields);
}

export function getElitePortfolioOptions() {
  return request<ElitePortfolioOptions>("/research/elite-portfolios/options", { cache: "no-store", timeoutMs: 60000 });
}

export type ResearchChampionStatus = {
  /** Distinct strategies the import would actually add — not raw job rows. */
  eligible_promoted_jobs: number;
  /** Eligible job rows examined, before duplicate strategies were collapsed. */
  eligible_jobs_scanned?: number;
  /** Rows whose strategy a live champion already covers, so they can never import. */
  duplicate_of_existing_champion?: number;
  duplicate_within_backlog?: number;
  symbols: number;
  timeframes: number;
  families: number;
  research_champions: number;
  final_elites: number;
  awaiting_forward_sample: number;
  pending_validation: number;
  validating: number;
  failed_validation: number;
  needs_more_data: number;
  graduated_elites: number;
  promotion_rule_version: string;
};

export type ResearchChampionImportResult = {
  imported: number;
  examined: number;
  dedupe_clusters_seen: number;
  already_covered_clusters: number;
  max_champions: number;
  promotion_rule_version: string;
  promotion_state: "research_champion";
  final_elites_created: 0;
  thresholds_weakened: false;
  champions: Array<Record<string, any>>;
  status: ResearchChampionStatus;
};

export type ChampionDedupeResult = {
  champions_examined: number;
  clusters_examined: number;
  duplicate_clusters: number;
  champions_demoted: number;
  champions_kept_per_cluster: Array<{ id: number; candidate_id: string; cluster_key: string; duplicates_demoted: number }>;
  dry_run: boolean;
  status: ResearchChampionStatus;
};

export function getResearchChampionStatus() {
  return request<ResearchChampionStatus>("/research/elite-portfolios/research-champions/status", { cache: "no-store", timeoutMs: 60000 });
}

// The backlog query is already bounded to actual eligible, not-yet-imported
// jobs, so passing this ceiling always means "import all of them" without the
// caller needing to know the exact backlog size. Matches the server-side cap
// on max_champions.
export const IMPORT_ALL_CHAMPIONS_LIMIT = 5000;

export function importResearchChampions(options: { maxChampions?: number; minProfitFactor?: number; minTrades?: number; maxDrawdown?: number } = {}) {
  const params = new URLSearchParams({
    max_champions: String(options.maxChampions ?? IMPORT_ALL_CHAMPIONS_LIMIT),
    min_profit_factor: String(options.minProfitFactor ?? 1.25),
    min_trades: String(options.minTrades ?? 30),
    max_drawdown: String(options.maxDrawdown ?? 0.12)
  });
  // No simulation runs here, only reads of already-stored results, but a
  // multi-thousand-row backlog is still slower than the default 2-minute
  // budget most calls on this page use.
  return request<ResearchChampionImportResult>(`/research/elite-portfolios/research-champions/import?${params.toString()}`, { method: "POST", timeoutMs: 300000 });
}

// Collapses champions that share an already-imported strategy's cluster key
// (see `_cluster_key` server-side) down to one representative per cluster.
// Only demotes -- never deletes, and never touches an already-graduated elite.
export function dedupeResearchChampions(options: { dryRun?: boolean } = {}) {
  const params = new URLSearchParams({ dry_run: String(options.dryRun ?? false) });
  return request<ChampionDedupeResult>(`/research/elite-portfolios/research-champions/dedupe?${params.toString()}`, { method: "POST", timeoutMs: 120000 });
}

export type ChampionValidationState =
  | "pending_validation"
  | "validating"
  | "validated"
  | "failed_validation"
  | "needs_more_data";

export type ChampionValidationQueueRow = {
  elite_candidate_id: number;
  candidate_id: string;
  campaign_id: number | null;
  symbol: string;
  timeframe: string;
  family_id: string;
  research_score: number;
  profit_factor: number;
  expectancy: number;
  max_drawdown: number;
  trade_count: number;
  validation_state: ChampionValidationState;
  validation_state_reason: string | null;
  dataset_id: number | null;
};

export type ChampionValidationQueue = {
  protocol_version: string;
  gates: Array<{ gate_id: string; label: string }>;
  thresholds: Record<string, number>;
  queue: ChampionValidationQueueRow[];
  research_champions: number;
  pending_validation: number;
  validating: number;
  failed_validation: number;
  needs_more_data: number;
  final_elites: number;
  graduated_elites: number;
};

export type ChampionValidationOutcome = {
  elite_candidate_id: number;
  candidate_id: string;
  symbol: string;
  timeframe: string;
  family_id?: string;
  run_id?: number;
  status: ChampionValidationState | "error";
  reason: string;
  gates_passed?: number;
  gates_failed?: number;
  gates_inconclusive?: number;
  backtests_executed?: number;
  runtime_ms?: number;
  failed_gates?: string[];
  inconclusive_gates?: string[];
};

export type ChampionValidationRunResult = {
  protocol_version: string;
  examined: number;
  validated: number;
  failed_validation: number;
  needs_more_data: number;
  errors: number;
  thresholds: Record<string, number>;
  thresholds_weakened: false;
  outcomes: ChampionValidationOutcome[];
  // Set when the run stopped on its wall-clock budget with champions still
  // queued. Call again to continue -- every verdict is already committed.
  budget_exhausted: boolean;
  remaining: number;
  runtime_seconds: number;
  status: ChampionValidationQueue;
};

export type ChampionValidationDiagnostics = {
  protocol_version: string;
  by_gate: Array<{ gate_id: string; label: string; status: string; candidates: number }>;
  by_group: Array<{
    family_id: string;
    symbol: string;
    timeframe: string;
    validated: number;
    failed_validation: number;
    needs_more_data: number;
    runs: number;
  }>;
  recent_runs: Array<Record<string, any>>;
};

export function getChampionValidationQueue(limit = 25) {
  return request<ChampionValidationQueue>(`/research/elite-portfolios/champion-validation/queue?limit=${limit}`, {
    cache: "no-store",
    timeoutMs: 60000
  });
}

export function getChampionValidationDiagnostics(limit = 25) {
  return request<ChampionValidationDiagnostics>(`/research/elite-portfolios/champion-validation/diagnostics?limit=${limit}`, {
    cache: "no-store",
    timeoutMs: 60000
  });
}

// The queue query is already bounded to champions in an eligible
// validation_state, so passing this ceiling always means "the whole queue"
// without the caller needing to know its exact size. Matches the server-side
// cap in ChampionValidationRequest.limit.
export const VALIDATE_ALL_CHAMPIONS_LIMIT = 2000;

export function runChampionValidation(options: { limit?: number; eliteCandidateIds?: number[]; revalidate?: boolean } = {}) {
  return request<ChampionValidationRunResult>("/research/elite-portfolios/champion-validation/run", {
    method: "POST",
    body: JSON.stringify({
      limit: options.limit ?? VALIDATE_ALL_CHAMPIONS_LIMIT,
      elite_candidate_ids: options.eliteCandidateIds ?? [],
      revalidate: options.revalidate ?? false
    }),
    // One call is bounded server-side by max_runtime_seconds, so this only has
    // to outlast a single budgeted batch rather than the whole queue -- the
    // caller drains the queue by calling repeatedly (see validateChampions in
    // ElitePortfolioBuilder), which is what keeps a long queue observable
    // instead of looking hung.
    timeoutMs: 600000
  });
}

export function previewElitePortfolio(configuration: ElitePortfolioConfiguration) {
  return request<ElitePortfolioResult>("/research/elite-portfolios/preview", { method: "POST", body: JSON.stringify(configuration), timeoutMs: 60000 });
}

export type EliteCorrelationEvidenceBackfill = {
  evidence_version: string;
  examined: number;
  generated: number;
  insufficient: number;
  failures: Array<{ research_job_id: number; error: string }>;
  remaining: boolean;
  historical_results_rewritten: false;
  constraints_relaxed: 0;
};

export type FamilyRegistryRow = {
  family_id: string;
  classification: string;
  status: "active" | "legacy";
  jobs: number;
  candidates: number;
  promoted_jobs: number;
  elites: number;
  median_profit_factor: number | null;
  avg_win_rate: number | null;
  avg_drawdown: number | null;
  avg_trades: number | null;
  avg_holding_hours: number | null;
  reason: string | null;
};

export function getFamilyRegistry(status?: "active" | "legacy") {
  const query = status ? `?status=${status}` : "";
  return request<{ families: FamilyRegistryRow[]; count: number }>(`/research/families/registry${query}`, { cache: "no-store", timeoutMs: 60000 });
}

export type IntradayTimeframeBreakdown = {
  timeframe: string;
  jobs: number;
  trades: number;
  avg_profit_factor: number | null;
  avg_expectancy: number | null;
  primary_rejection_reasons: Array<{ reason: string; occurrences: number }>;
  status: "has_evidence" | "not_started";
};

export type IntradaySampleJob = {
  symbol: string;
  timeframe: string;
  direction: string | null;
  variant_parameter: string | null;
  status: string;
  validation_score: number | null;
  trades: number | null;
  profit_factor: number | null;
  expectancy: number | null;
  failure_reasons: string[];
};

export type IntradayPilotSummary = {
  campaign_id: number;
  name: string;
  status: string;
  jobs: number;
  trades: number;
  promoted: number;
  outcome: string;
};

export type IntradayStrategyRosterEntry = {
  id: string;
  name: string;
  version: string | null;
  status: "archived" | "planned" | "active";
  reason: string | null;
  summary: string | null;
  jobs?: number;
  trades?: number;
  campaigns?: number;
  promoted?: number;
  pilot?: IntradayPilotSummary | null;
  timeframe_breakdown?: IntradayTimeframeBreakdown[];
  sample_jobs?: IntradaySampleJob[];
};

export type IntradayLabOverview = {
  infrastructure_status: string;
  timeframes_supported: string[];
  strategies: IntradayStrategyRosterEntry[];
  forward_validation_note: string;
};

export function getIntradayLabOverview() {
  return request<IntradayLabOverview>("/research/intraday/overview", { cache: "no-store", timeoutMs: 30000 });
}

export type IntradayCampaignPlan = {
  plan_version: string;
  active_family_count: number;
  active_families: { architecture: string; name: string; status: string; supported_timeframes: string[] }[];
  asset_count: number;
  assets: string[];
  timeframes_supported: string[];
  timeframes_selected: string[];
  variants_per_family: number;
  candidates_generated: number;
  candidates_after_dedupe: number;
  estimated_jobs: number;
  duplicate_of_campaign_id: number | null;
  requires_rerun_confirmation: boolean;
  signal_diagnostics: {
    measured_families: number;
    verdict_counts: Record<string, number>;
    predictive: string[];
    signal_below_cost: string[];
  };
  protocol: {
    split_protocol_version: string;
    elite_gate_version: string;
    cost_model: { version: string; fee_rate_per_leg: number; slippage_rate_per_leg: number; round_trip_rate: number };
    simulator_audit: { audit_version: string; simulator_sound: boolean; defects: string[]; economics_findings: string[] };
  };
  blockers: { code: string; detail: string }[];
  warnings: { code: string; detail: string }[];
  can_launch: boolean;
  evidence_policy: string;
};

/** Resolved server-side. Never recompute the job count in the frontend. */
export function getIntradayCampaignPlan(options: { timeframes: string[]; assetLimit?: number; variantsPerFamily?: number }) {
  const params = new URLSearchParams();
  for (const timeframe of options.timeframes) params.append("timeframes", timeframe);
  params.append("asset_limit", String(options.assetLimit ?? 10));
  params.append("variants_per_family", String(options.variantsPerFamily ?? 12));
  return request<IntradayCampaignPlan>(`/research/intraday/campaign-plan?${params.toString()}`, {
    cache: "no-store",
    timeoutMs: 30000
  });
}

export type IntradayBroadScreenResult = Record<string, any> & { plan: IntradayCampaignPlan };

/** Families are resolved server-side from the registry, never sent by the client. */
export function launchIntradayBroadScreen(options: {
  timeframes: string[];
  assetLimit?: number;
  variantsPerFamily?: number;
  name?: string;
  /** Confirms re-running a configuration that already ran, as its own campaign. */
  allowRerun?: boolean;
}) {
  const params = new URLSearchParams();
  for (const timeframe of options.timeframes) params.append("timeframes", timeframe);
  params.append("asset_limit", String(options.assetLimit ?? 10));
  params.append("variants_per_family", String(options.variantsPerFamily ?? 12));
  if (options.name) params.append("name", options.name);
  if (options.allowRerun) params.append("allow_rerun", "true");
  return request<IntradayBroadScreenResult>(`/research/intraday/campaigns/broad-screen?${params.toString()}`, {
    method: "POST",
    timeoutMs: 120000
  });
}

export type IntradaySignalDiagnostic = {
  architecture: string;
  family_name: string | null;
  timeframe: string;
  verdict: "predictive" | "signal_below_cost" | "no_signal" | "insufficient_signals" | "not_measurable";
  detail: string;
  best_horizon_bars: number | null;
  excess_edge_bps: number | null;
  t_statistic: number | null;
  round_trip_cost_bps: number;
  clears_cost: boolean;
  signal_count: number;
  horizons: {
    horizon_bars: number;
    signals: number;
    raw_edge_bps: number;
    unconditional_drift_bps: number;
    excess_edge_bps: number;
    t_statistic: number | null;
    hit_rate: number;
  }[];
};

/** Stored verdicts only — rendering a row never triggers a recompute. */
export async function getIntradaySignalDiagnostics(timeframe?: string): Promise<IntradaySignalDiagnostic[] | null> {
  const params = new URLSearchParams();
  if (timeframe) params.append("timeframe", timeframe);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  try {
    const response = await request<{ diagnostics: IntradaySignalDiagnostic[] }>(
      `/research/intraday/signal-diagnostics${suffix}`,
      { cache: "no-store", timeoutMs: 30000 }
    );
    return response.diagnostics ?? [];
  } catch {
    return null;
  }
}

export type IntradaySignalDiagnosticsJob = {
  id: number;
  status: "queued" | "running" | "completed" | "failed";
  progress_total?: number;
  progress_completed?: number;
  progress_current?: string | null;
  result?: Record<string, any> | null;
  error?: string | null;
  queue?: {
    queued: number;
    running: number;
    oldest_queued_seconds: number | null;
    worker_appears_stopped: boolean;
    detail: string;
  };
};

/** Queues the measurement and returns immediately — the actual run happens in
 * a background worker, not in this request. See enqueue_signal_diagnostics_job. */
export function enqueueIntradaySignalDiagnostics(options: {
  timeframe: string;
  maxVariants?: number;
  maxSymbols?: number;
}) {
  const params = new URLSearchParams();
  params.append("timeframe", options.timeframe);
  params.append("max_variants", String(options.maxVariants ?? 3));
  params.append("max_symbols", String(options.maxSymbols ?? 4));
  return request<{ job_id: number; status: string }>(
    `/research/intraday/signal-diagnostics?${params.toString()}`,
    { method: "POST", timeoutMs: 30000 }
  );
}

export function getIntradaySignalDiagnosticsJob(jobId: number) {
  return request<IntradaySignalDiagnosticsJob>(`/research/intraday/signal-diagnostics/jobs/${jobId}`, {
    cache: "no-store",
    timeoutMs: 30000
  });
}

/** Queues the measurement, then polls until it finishes.
 *
 * Fails fast and specifically when nothing is consuming the queue: a job that
 * never leaves `queued` means the worker process is not running, which is a
 * different problem from a slow measurement and should not be reported as a
 * ten-minute timeout. See app/workers/signal_diagnostics_runner.py. */
export async function runIntradaySignalDiagnostics(
  options: { timeframe: string; maxVariants?: number; maxSymbols?: number },
  onStatus?: (job: IntradaySignalDiagnosticsJob) => void
): Promise<IntradaySignalDiagnosticsJob> {
  const { job_id } = await enqueueIntradaySignalDiagnostics(options);
  const deadline = Date.now() + 10 * 60 * 1000;
  while (Date.now() < deadline) {
    const job = await getIntradaySignalDiagnosticsJob(job_id);
    onStatus?.(job);
    if (job.status === "completed" || job.status === "failed") return job;
    if (job.queue?.worker_appears_stopped) {
      throw new Error(
        `No worker is consuming the signal-diagnostics queue, so job ${job_id} will never run. ` +
          `Start it with: python -m app.workers.signal_diagnostics_runner`
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error(`Signal diagnostics job ${job_id} did not finish within 10 minutes.`);
}

export type IntradayFamilyDiagnostic = {
  architecture: string;
  failure_reason: string | null;
  recommendation: string | null;
  buried: boolean;
};

/** Phase F diagnostic state per family. Returns null when unavailable. */
export async function getIntradayFamilyDiagnostics(campaignId: number | null): Promise<IntradayFamilyDiagnostic[] | null> {
  if (campaignId == null) return null;
  try {
    const report = await request<Record<string, any>>(`/research/intraday/campaigns/${campaignId}/diagnostics`, {
      cache: "no-store",
      timeoutMs: 60000
    });
    return (report.families ?? []).map((family: any) => ({
      architecture: family.architecture,
      failure_reason: family.diagnosis?.failure_reason ?? null,
      recommendation: family.next_experiment?.recommendation ?? null,
      buried: false
    }));
  } catch {
    return null;
  }
}

export type IntradayExpansionRecommendation = {
  available: boolean;
  reason: string | null;
  families: { architecture: string; family_name: string | null; screen_score: number }[];
};

/** The backend's current near-pass recommendation; null when it has none. */
export async function getIntradayExpansionRecommendation(campaignId: number | null): Promise<IntradayExpansionRecommendation | null> {
  if (campaignId == null) return null;
  try {
    const report = await request<Record<string, any>>(`/research/intraday/campaigns/${campaignId}/family-ranking`, {
      cache: "no-store",
      timeoutMs: 60000
    });
    const promising = (report.families ?? []).filter((family: any) => family.promising);
    return {
      available: promising.length > 0,
      reason: report.next_step ?? null,
      families: promising.slice(0, 3).map((family: any) => ({
        architecture: family.architecture,
        family_name: family.family_name ?? null,
        screen_score: family.screen_score ?? 0
      }))
    };
  } catch {
    return null;
  }
}

export function createIntradayCampaign(options: {
  familyIds: string[];
  name?: string;
  assetLimit?: number;
  timeframes?: string[];
  maxCandidatesPerFamily?: number;
}) {
  const params = new URLSearchParams();
  for (const familyId of options.familyIds) params.append("family_ids", familyId);
  if (options.name) params.append("name", options.name);
  params.append("asset_limit", String(options.assetLimit ?? 10));
  params.append("max_candidates_per_family", String(options.maxCandidatesPerFamily ?? 8));
  for (const timeframe of options.timeframes ?? []) params.append("timeframes", timeframe);
  return request<Record<string, any>>(`/research/intraday/campaigns?${params.toString()}`, { method: "POST", timeoutMs: 120000 });
}

export function launchLowTimeframeExpansion(options: {
  name?: string;
  parentLimit?: number;
  variantsPerParent?: number;
  assetLimit?: number;
  timeframes?: string[];
  preferredFamily?: string;
  workers?: number;
  jobsPerWorker?: number;
} = {}) {
  const params = new URLSearchParams();
  if (options.name) params.append("name", options.name);
  params.append("parent_limit", String(options.parentLimit ?? 64));
  params.append("variants_per_parent", String(options.variantsPerParent ?? 12));
  params.append("asset_limit", String(options.assetLimit ?? 4));
  params.append("preferred_family", options.preferredFamily ?? "Momentum");
  params.append("workers", String(options.workers ?? 4));
  params.append("jobs_per_worker", String(options.jobsPerWorker ?? 50));
  for (const timeframe of options.timeframes ?? ["30m"]) params.append("timeframes", timeframe);
  return request<Record<string, any>>(`/research/intraday/campaigns/low-timeframe-expansion?${params.toString()}`, { method: "POST", timeoutMs: 120000 });
}

export type Phase124GroupMetrics = {
  trade_count: number;
  gross_profit: number;
  gross_loss: number;
  gross_pnl: number;
  fees: number;
  slippage_cost: number;
  total_transaction_costs: number;
  net_pnl: number;
  gross_profit_factor: number | null;
  net_profit_factor: number | null;
  gross_expectancy: number;
  net_expectancy: number;
  win_rate: number;
  average_win: number;
  average_loss: number;
  payoff_ratio: number | null;
  median_trade_return_pct: number;
};

export type Phase124Performance = Phase124GroupMetrics & {
  total_jobs: number;
  median_job_profit_factor: number | null;
  median_job_expectancy: number | null;
  cost_impact_pct_of_gross_expectancy: number | null;
  verdict: string;
};

export type Phase124ExitRow = Phase124GroupMetrics & {
  exit_reason: string;
  pct_of_trades: number;
  average_holding_period_hours: number;
  median_holding_period_hours: number;
  average_mfe: number | null;
  median_mfe: number | null;
  average_mae: number | null;
  median_mae: number | null;
  average_remaining_session_minutes_at_entry: number | null;
};

export type Phase124SubgroupRow = Phase124GroupMetrics & {
  key: string;
  meets_minimum_evidence: boolean;
  stability_notes: string[];
};

export type Phase124FamilyReport = {
  architecture: string;
  family_name: string;
  performance_decomposition: Phase124Performance;
  exit_reason_breakdown: Phase124ExitRow[];
  entry_quality_analysis: Record<string, any>;
  cost_and_position_sizing_analysis: Record<string, any>;
  stability_analysis: {
    campaign_level_dominance: Record<string, any>;
    by_symbol: Phase124SubgroupRow[];
    by_direction: Phase124SubgroupRow[];
    by_timeframe: Phase124SubgroupRow[];
    by_month: Phase124SubgroupRow[];
    by_exit_reason: Phase124SubgroupRow[];
    by_candidate_parameter_set: Phase124SubgroupRow[];
    by_relative_volume_bucket: Phase124SubgroupRow[];
    by_market_regime: { rows: Array<{ key: string } & Phase124GroupMetrics>; insufficient_evidence: string };
  };
  failure_classifications: Array<{ classification: string; evidence: Record<string, any> }>;
  research_allocation: {
    family: string;
    decision: "retain_for_focused_investigation" | "redesign_as_separately_versioned_hypothesis" | "gather_more_evidence" | "archive";
    primary_evidence: Record<string, any>;
    principal_failure_mechanism: string[];
    strongest_subgroup: string | null;
    weakest_subgroup: string | null;
    evidence_stability: "stable" | "not_stable";
    recommended_research_budget: string;
    permitted_next_action: string;
    prohibited_next_action: string;
  };
};

export type Phase124Report = {
  campaign_id: number;
  minimum_evidence_rules: Record<string, number>;
  entry_time_buckets: string[];
  relative_volume_buckets: string[];
  data_availability: Record<string, string>;
  families: Phase124FamilyReport[];
  amd_30m_session_momentum_investigation: Record<string, any>;
};

export type StrategyDnaRecord = {
  id: number;
  family_architecture: string;
  strategy_version: string;
  dna_schema_version: number;
  fingerprint: string;
  dna: Record<string, any>;
  superseded_by_id: number | null;
  created_at: string;
};

export type StrategyDnaResponse = {
  dna_schema_version: number;
  families: StrategyDnaRecord[];
  behavioral_similarity: Array<{ a: string; b: string; behavioral_similarity: number }>;
};

export type Phase13FamilyAnalytics = {
  architecture: string;
  jobs: number;
  promoted_jobs: number;
  promotion_rate: number;
  symbols: number;
  trades: number;
  trades_per_job: number;
  avg_profit_factor: number | null;
  avg_expectancy: number | null;
  avg_max_drawdown: number | null;
  avg_total_return: number | null;
  avg_holding_hours: number | null;
  evidence_tier: "statistically_reliable" | "descriptive" | "exploratory" | "insufficient_sample";
  failure_by_validation_rule: Array<{ validation_rule: string; occurrences: number }>;
};

export type Phase13CampaignAnalytics = {
  analytics_version: string;
  campaign_id: number;
  evidence_tier_rules: Record<string, string>;
  families: Phase13FamilyAnalytics[];
  holding_period_distribution: Array<Record<string, any>>;
  breakdowns: Record<string, Array<Record<string, any>>>;
  candidate_buckets: {
    profitable_but_under_evidenced: Array<Record<string, any>>;
    frequent_but_unprofitable: Array<Record<string, any>>;
    near_pass: Array<Record<string, any>>;
  };
  family_confidence_intervals: Array<Record<string, any>>;
  dna_diversity: Record<string, any>;
  causal_claims_disclaimer: string;
};

export function getStrategyDna() {
  return request<StrategyDnaResponse>("/research/strategy-dna", { cache: "no-store", timeoutMs: 30000 });
}

export function getPhase13Analytics(campaignId: number) {
  return request<Phase13CampaignAnalytics>(`/research/intraday/analytics/${campaignId}`, { cache: "no-store", timeoutMs: 60000 });
}

export function getCandidateEvidenceReport(campaignId: number, candidateId: string) {
  return request<Record<string, any>>(
    `/research/intraday/evidence-report/${campaignId}/${encodeURIComponent(candidateId)}`,
    { cache: "no-store", timeoutMs: 30000 }
  );
}

export function getPhase124Analysis(campaignId: number) {
  return request<Phase124Report>(`/research/intraday/phase-12-4?campaign_id=${campaignId}`, { cache: "no-store", timeoutMs: 30000 });
}

export function refreshFamilyRegistry() {
  return request<{ families: number; active: number; legacy: number; by_classification: Record<string, number>; evidence_deleted: boolean }>(
    "/research/families/refresh-registry",
    { method: "POST", timeoutMs: 120000 }
  );
}

export function launchHighFrequencyCampaign(maxCandidates = 120, timeframes: string[] = ["15m", "30m"]) {
  const params = new URLSearchParams({ max_candidates: String(maxCandidates) });
  for (const timeframe of timeframes) params.append("timeframes", timeframe);
  return request<Record<string, any>>(`/research/campaigns/high-frequency?${params.toString()}`, { method: "POST", timeoutMs: 120000 });
}

export function launchHiddenGemRecovery(maxFamilies = 27) {
  return request<Record<string, any>>(`/research/campaigns/hidden-gem-recovery?max_families=${maxFamilies}`, { method: "POST", timeoutMs: 120000 });
}

export function reevaluateElites() {
  return request<{ evaluated: number; retained_count: number; demoted_count: number; rule_version: string }>(
    "/research/elite-candidates/reevaluate",
    { method: "POST", timeoutMs: 120000 }
  );
}

export function backfillElitePortfolioEvidence(limit = 20) {
  return request<EliteCorrelationEvidenceBackfill>(`/research/elite-portfolios/evidence/backfill?limit=${limit}`, { method: "POST", timeoutMs: 120000 });
}

export function createElitePortfolio(configuration: ElitePortfolioConfiguration) {
  return request<ElitePortfolioResult>("/research/elite-portfolios", { method: "POST", body: JSON.stringify(configuration), timeoutMs: 60000 });
}

export function approveElitePortfolio(portfolioId: number, snapshotHash: string) {
  return request<ElitePortfolioResult>(`/research/elite-portfolios/${portfolioId}/approve`, { method: "POST", body: JSON.stringify({ snapshot_hash: snapshotHash }), timeoutMs: 60000 });
}

export function activateElitePortfolio(portfolioId: number, snapshotHash: string, idempotencyKey: string) {
  return request<ElitePortfolioResult>(`/research/elite-portfolios/${portfolioId}/activate-internal`, { method: "POST", body: JSON.stringify({ snapshot_hash: snapshotHash, idempotency_key: idempotencyKey }), timeoutMs: 60000 });
}

/**
 * Apply a portfolio profile to a configuration.
 *
 * The visible constraint values must be replaced, not just the profile name.
 * `normalized_configuration` merges explicit `constraints` *over* the profile
 * preset, so a configuration that carries the strict defaults alongside
 * `profile: "single_elite_test"` silently builds a strict portfolio -- which is
 * exactly what happened before this existed.
 */
export function configurationWithProfile(
  configuration: ElitePortfolioConfiguration,
  profile: ElitePortfolioProfile
): ElitePortfolioConfiguration {
  return { ...configuration, profile: profile.id, constraints: { ...profile.resolved_constraints } };
}

// Every mode "All Validated Elites Paper Lab" sets on a run's stored
// configuration. Kept as a literal here (not fetched from the backend) so a
// frontend build fails loudly if it ever drifts from the Python constant of
// the same name in elite_portfolio_builder.py.
export const PAPER_LAB_MODE = "all_validated_elites_paper_lab";

export type ElitePortfolioRunSummary = {
  id: number;
  run_key: string;
  status: string;
  objective: string | null;
  snapshot_hash: string | null;
  approved_snapshot_hash: string | null;
  approved_at: string | null;
  activated_at: string | null;
  profile: string | null;
  mode: string | null;
  diversified: boolean;
  member_count: number;
};

export type ElitePortfolioRunList = {
  runs: ElitePortfolioRunSummary[];
  activatable: ElitePortfolioRunSummary[];
  current_activatable_run_id: number | null;
};

export function getElitePortfolioRuns(limit = 20) {
  return request<ElitePortfolioRunList>(`/research/elite-portfolios/runs?limit=${limit}`, { cache: "no-store", timeoutMs: 60000 });
}

export function getElitePortfolioRecommendation() {
  return request<ElitePortfolioRecommendation>("/research/elite-portfolios/profile-recommendation", { cache: "no-store", timeoutMs: 120000 });
}

// --- All Validated Elites Paper Lab ------------------------------------------
//
// An execution-testing mode, not a diversified portfolio: every validated
// elite that can reach Alpaca Paper is included, correlated or not. Reuses
// the same ElitePortfolioResult shape as the diversified preview/create
// endpoints (mode/diversified/warning distinguish it), and the same
// run/activation infrastructure once saved -- Step 04 does not need to know
// which path a run came from.

export function getPaperLabPreview() {
  return request<ElitePortfolioResult>("/research/elite-portfolios/paper-lab/preview", { cache: "no-store", timeoutMs: 120000 });
}

export function createPaperLabRun() {
  return request<ElitePortfolioResult>("/research/elite-portfolios/paper-lab", { method: "POST", timeoutMs: 120000 });
}

// --- Step 04: activation -----------------------------------------------------

export type PreflightCheck = { code: string; label: string; passed: boolean; detail: string };

export type ExecutionPreflight = {
  external_deployment_id: number;
  state: string;
  state_label: string;
  checks: PreflightCheck[];
  passed: boolean;
  outstanding: string[];
  next_action: string;
  account_environment: string;
  live_money_supported: false;
  active_halts: Array<Record<string, any>>;
};

export type ActivationMemberAction = { action: string; enabled: boolean; reason: string };

export type ActivationMember = {
  id: number;
  rank: number;
  candidate_id: string;
  symbol: string;
  timeframe: string;
  strategy_family?: string | null;
  family_id?: string | null;
  strategy_direction: string;
  execution_capability: string;
  activation_state: string;
  latest_error: string | null;
  internal_deployment_id: number | null;
  external_deployment_id: number | null;
  internal_deployment_state: string;
  external_deployment_state: string;
  external_deployment_state_label: string;
  preflight: ExecutionPreflight | null;
  activity: {
    last_scan: Record<string, any> | null;
    last_signal: Record<string, any> | null;
    last_risk_decision: Record<string, any> | null;
    last_proposed_order: Record<string, any> | null;
    last_submitted_order: Record<string, any> | null;
    last_fill: Record<string, any> | null;
    halt_reason: string | null;
  };
  available_actions: ActivationMemberAction[];
};

export type ActivationSafetyPanel = {
  provider: string;
  environment: string;
  account_is_paper: boolean;
  live_money_supported: false;
  account: Record<string, any>;
  broker_sync: Record<string, any> | null;
  reconciliation: Record<string, any> | null;
  market_clock: Record<string, any>;
  active_halts: Array<Record<string, any>>;
  feature_flags: Record<string, boolean>;
  risk_limits: Record<string, number | boolean>;
};

export type PortfolioActivationView = {
  portfolio_run_id: number;
  run_key: string | null;
  status: string;
  snapshot_hash: string | null;
  approved_snapshot_hash: string | null;
  approved_at: string | null;
  activated_at: string | null;
  objective: string | null;
  profile: string | null;
  // Set only for an "All Validated Elites Paper Lab" run. `diversified`
  // defaults true server-side for any run that never set it, so it is always
  // safe to read directly -- a diversified run and a paper lab run can never
  // be confused for one another here.
  mode: string | null;
  diversified: boolean;
  warning: string | null;
  members: ActivationMember[];
  activation_attempts: Array<Record<string, any>>;
  summary: {
    member_count: number;
    internally_active: number;
    external_records: number;
    observe_only: number;
    execution_enabled: number;
    preflight_ready: number;
  };
  safety: ActivationSafetyPanel;
  live_money_supported: false;
};

export function getPortfolioActivation(portfolioId: number) {
  return request<PortfolioActivationView>(`/research/elite-portfolios/${portfolioId}/activation`, { cache: "no-store", timeoutMs: 120000 });
}

export function approveMemberForAlpacaPaper(portfolioId: number, memberId: number, options: { reapprove?: boolean } = {}) {
  return request<Record<string, any>>(`/research/elite-portfolios/${portfolioId}/members/${memberId}/approve-external-paper`, {
    method: "POST",
    body: JSON.stringify({ reapprove: options.reapprove ?? false }),
    timeoutMs: 120000
  });
}

export function enableMemberPaperExecution(portfolioId: number, memberId: number) {
  return request<Record<string, any>>(`/research/elite-portfolios/${portfolioId}/members/${memberId}/enable-paper-execution`, {
    method: "POST",
    // Repeats the member id, mirroring the CLI's --confirm-deployment-id: this
    // is the last approval before real orders reach a broker.
    body: JSON.stringify({ confirm_member_id: memberId }),
    timeoutMs: 120000
  });
}

export type BulkApprovalResult = {
  portfolio_run_id: number;
  approved: Array<Record<string, any>>;
  skipped: Array<{ member_id: number; reason: string }>;
  errors: Array<{ member_id: number; error: string }>;
  summary: { approved: number; skipped: number; errors: number; total: number };
  live_money_supported: false;
};

export type BulkExecutionResult = {
  portfolio_run_id: number;
  enabled: Array<Record<string, any>>;
  blocked: Array<{ member_id: number; reason: string }>;
  errors: Array<{ member_id: number; error: string }>;
  summary: { enabled: number; blocked: number; errors: number; total: number };
  live_money_supported: false;
};

export function approveAllMembersForAlpacaPaper(portfolioId: number, options: { reapprove?: boolean } = {}) {
  return request<BulkApprovalResult>(`/research/elite-portfolios/${portfolioId}/members/approve-all-external-paper`, {
    method: "POST",
    // Repeats the portfolio run id: a bulk action touches every member at
    // once, so it gets the same explicit-confirmation treatment as a single
    // execution-enable click, not less.
    body: JSON.stringify({ reapprove: options.reapprove ?? false, confirm_portfolio_run_id: portfolioId }),
    timeoutMs: 300000
  });
}

export function enableAllReadyMembersPaperExecution(portfolioId: number) {
  return request<BulkExecutionResult>(`/research/elite-portfolios/${portfolioId}/members/enable-all-paper-execution`, {
    method: "POST",
    body: JSON.stringify({ confirm_portfolio_run_id: portfolioId }),
    timeoutMs: 300000
  });
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

export function fetchResearchCommandCenter(filters: ResearchCommandCenterFilters = {}) {
  const params = new URLSearchParams();
  if (filters.campaignId) params.set("campaign_id", String(filters.campaignId));
  if (filters.asset) params.set("asset", filters.asset);
  if (filters.assetClass) params.set("asset_class", filters.assetClass);
  if (filters.timeframe) params.set("timeframe", filters.timeframe);
  if (filters.strategyFamily) params.set("strategy_family", filters.strategyFamily);
  if (filters.candidateState) params.set("candidate_state", filters.candidateState);
  if (filters.validationRule) params.set("validation_rule", filters.validationRule);
  if (filters.regime) params.set("regime", filters.regime);
  if (filters.dateFrom) params.set("date_from", filters.dateFrom);
  if (filters.dateTo) params.set("date_to", filters.dateTo);
  const suffix = params.size ? `?${params.toString()}` : "";
  return request<Partial<ResearchCommandCenter>>(`/research/command-center${suffix}`, { timeoutMs: 60000 })
    .then(normalizeResearchCommandCenter);
}

function normalizeResearchCommandCenter(value: Partial<ResearchCommandCenter>): ResearchCommandCenter {
  const array = <T>(candidate: T[] | undefined | null): T[] => Array.isArray(candidate) ? candidate : [];
  const object = <T extends Record<string, any>>(candidate: T | undefined | null): T => candidate && typeof candidate === "object" && !Array.isArray(candidate) ? candidate : {} as T;
  const intelligence = (candidate: ResearchCommandCenter["strategy_intelligence"] | undefined) => ({
    rows: array(candidate?.rows),
    highlights: object(candidate?.highlights)
  });
  const proposal = value.next_campaign_proposal && typeof value.next_campaign_proposal === "object"
    ? {
        ...value.next_campaign_proposal,
        strategy_families_to_retain: array(value.next_campaign_proposal.strategy_families_to_retain),
        strategy_families_to_deprioritize: array(value.next_campaign_proposal.strategy_families_to_deprioritize),
        assets_to_retain: array(value.next_campaign_proposal.assets_to_retain),
        assets_to_deprioritize: array(value.next_campaign_proposal.assets_to_deprioritize),
        timeframes_to_retain: array(value.next_campaign_proposal.timeframes_to_retain),
        timeframes_to_deprioritize: array(value.next_campaign_proposal.timeframes_to_deprioritize),
        new_hypothesis_tests: array(value.next_campaign_proposal.new_hypothesis_tests)
      }
    : null;
  return {
    live_evidence: Boolean(value.live_evidence),
    campaign: value.campaign ?? null,
    campaigns: array(value.campaigns),
    filters: object(value.filters),
    filter_options: object(value.filter_options),
    overview: object(value.overview),
    candidate_funnel: array(value.candidate_funnel),
    rejection_analysis: object(value.rejection_analysis),
    near_pass_candidates: array(value.near_pass_candidates).map((row) => ({ ...row, failed_gates: array(row.failed_gates) })),
    strategy_intelligence: intelligence(value.strategy_intelligence),
    asset_intelligence: intelligence(value.asset_intelligence),
    timeframe_intelligence: intelligence(value.timeframe_intelligence),
    regime_analysis: array(value.regime_analysis),
    duplicate_analysis: object(value.duplicate_analysis),
    experiment_history: array(value.experiment_history).map((row) => ({ ...row, assets: array(row.assets), timeframes: array(row.timeframes), failure_reasons: array(row.failure_reasons) })),
    recommendations: array(value.recommendations),
    next_campaign_proposal: proposal,
    historical_research: object(value.historical_research),
    terminology: object(value.terminology),
    reconciliation: object(value.reconciliation),
    source: object(value.source),
    simulation_only: value.simulation_only !== false
  };
}

export function getPersistedCandidateProfile(candidateId: string, options?: { campaignId?: number }) {
  const params = new URLSearchParams();
  if (options?.campaignId) params.set("campaign_id", String(options.campaignId));
  const suffix = params.size ? `?${params.toString()}` : "";
  return request<PersistedCandidateProfile>(`/research/candidates/${encodeURIComponent(candidateId)}${suffix}`, { timeoutMs: 60000 });
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

export function getStrategyDiscoveryDashboard(options?: { limit?: number }) {
  const params = new URLSearchParams({ limit: String(options?.limit ?? 20) });
  return request<StrategyDiscoveryDashboard>(`/research/strategy-discovery/dashboard?${params.toString()}`, { timeoutMs: 60000 });
}

export function runStrategyDiscovery(input: ResearchAssetInput & { maxCandidates?: number } = { symbol: "BTCUSDT", timeframe: "4h" }) {
  const params = new URLSearchParams({
    symbol: input.symbol,
    timeframe: input.timeframe ?? "4h",
    max_candidates: String(input.maxCandidates ?? 50)
  });
  return request<StrategyDiscoveryRun>(`/research/strategy-discovery/run?${params.toString()}`, { method: "POST", timeoutMs: 240000 });
}

export function evolveStrategyDiscovery(limit = 20) {
  return request<Record<string, unknown>>(`/research/strategy-discovery/evolve?limit=${limit}`, { method: "POST", timeoutMs: 60000 });
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
  return request<SymbolRow[]>("/symbols", { timeoutMs: 30000 });
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
  return request<ResearchIntelligence>("/research/intelligence", { timeoutMs: 60000 });
}

export function getResearchLearning() {
  return request<ResearchLearningSummary>("/research/learning", { timeoutMs: 60000 });
}

export function saveResearchUniverse(payload: ResearchUniverseInput) {
  return request<Record<string, unknown>>("/research/universes", {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: 120000
  });
}

export function syncAlpacaAssetCatalog() {
  return request<AlpacaAssetCatalog>("/data/alpaca/assets/sync", {
    method: "POST",
    timeoutMs: 60000
  });
}

export function createResearchCampaign(options: {
  universeKey: string;
  name: string;
  maxCandidates: number;
  assetLimit: number;
  timeframes: string[];
  architectureMode?: "intelligent" | "legacy";
  datasetMode?: "rolling" | "reproducibility";
  searchMode?: "scout_expand" | "full";
}) {
  const params = new URLSearchParams({
    universe_key: options.universeKey,
    name: options.name,
    max_candidates: String(options.maxCandidates),
    asset_limit: String(options.assetLimit),
    architecture_mode: options.architectureMode ?? "intelligent",
    dataset_mode: options.datasetMode ?? "rolling",
    search_mode: options.searchMode ?? (options.architectureMode === "legacy" ? "scout_expand" : "full")
  });
  for (const timeframe of options.timeframes) params.append("timeframes", timeframe);
  return request<ResearchCampaignCreateResult>(`/research/campaigns?${params.toString()}`, {
    method: "POST",
    timeoutMs: 300000
  });
}

export function preflightResearchCampaign(assets: string[], timeframes: string[]) {
  return request<ResearchCampaignPreflight>("/research/campaigns/preflight", {
    method: "POST",
    body: JSON.stringify({ assets, timeframes }),
    timeoutMs: 60000
  });
}

export function prepareResearchCampaign(assets: string[], timeframes: string[]) {
  return request<ResearchCampaignPreparation>("/research/campaigns/prepare", {
    method: "POST",
    body: JSON.stringify({ assets, timeframes }),
    timeoutMs: 900000
  });
}

export function getResearchArchitecture(datasetId?: number) {
  const params = new URLSearchParams();
  if (datasetId) params.set("dataset_id", String(datasetId));
  const query = params.toString();
  return request<ResearchArchitectureState>(`/research/architecture${query ? `?${query}` : ""}`, { timeoutMs: 60000 });
}

export function runAutonomousResearchCycle(options: {
  universeKey?: string;
  timeframes?: string[];
  maxCandidates?: number;
  assetLimit?: number;
  datasetMode?: "rolling" | "reproducibility";
  approvalMode?: "manual" | "auto_queue";
}) {
  return request<Record<string, unknown>>("/research/architecture/cycles", {
    method: "POST",
    body: JSON.stringify({
      universe_key: options.universeKey ?? "research_core_ten",
      timeframes: options.timeframes,
      max_candidates: options.maxCandidates ?? 250,
      asset_limit: options.assetLimit ?? 10,
      dataset_mode: options.datasetMode ?? "rolling",
      approval_mode: options.approvalMode ?? "manual"
    }),
    timeoutMs: 300000
  });
}

export function verifyResearchDataset(datasetId: number) {
  return request<Record<string, unknown>>(`/research/datasets/${datasetId}/verify`, { method: "POST", timeoutMs: 60000 });
}

export function exportResearchDataset(datasetId: number) {
  return request<Record<string, unknown>>(`/research/datasets/${datasetId}/export`, { method: "POST", timeoutMs: 300000 });
}

export function runResearchCampaignBatch(campaignId: number, batchSize = 50) {
  const params = new URLSearchParams({ batch_size: String(batchSize) });
  return request<ResearchCampaignBatchResult>(`/research/campaigns/${campaignId}/run?${params.toString()}`, {
    method: "POST",
    timeoutMs: 300000
  });
}

export function getResearchCampaign(campaignId: number) {
  return request<ResearchCampaignStatus>(`/research/campaigns/${campaignId}`, { cache: "no-store", timeoutMs: 60000 });
}

export function getResearchCampaigns(limit = 50) {
  return request<ResearchCampaignList>(`/research/campaigns?limit=${limit}`, { cache: "no-store", timeoutMs: 60000 });
}

export function controlResearchCampaign(campaignId: number, action: "pause" | "resume") {
  return request<ResearchCampaignStatus>(`/research/campaigns/${campaignId}/control?action=${action}`, {
    method: "POST",
    timeoutMs: 60000
  });
}

export function repairResearchCampaign(campaignId: number) {
  return request<{ campaign_id: number; actions: Record<string, number>; reopened: boolean; finalized: boolean; repair_resolved: boolean }>(`/research/campaigns/${campaignId}/repair`, {
    method: "POST",
    timeoutMs: 120000
  });
}

export function deleteResearchCampaign(campaignId: number, force = false) {
  return request<{ deleted: boolean; campaign_id: number; name: string; deleted_jobs: number; deleted_evidence_jobs: number; forced: boolean }>(`/research/campaigns/${campaignId}?force=${force}`, {
    method: "DELETE",
    timeoutMs: 60000
  });
}

export function getResearchCampaignProfile(campaignId: number) {
  return request<ResearchCampaignProfile>(`/research/campaigns/${campaignId}/profile`, { cache: "no-store", timeoutMs: 60000 });
}

export function runParallelResearchCampaign(campaignId: number, workers = 4, jobsPerWorker = 25) {
  const params = new URLSearchParams({ workers: String(workers), jobs_per_worker: String(jobsPerWorker) });
  return request<{ campaign_id: number; started: boolean; already_active: boolean; workers: number; jobs_per_worker: number; remaining: number }>(`/research/campaigns/${campaignId}/run-parallel?${params.toString()}`, {
    method: "POST",
    timeoutMs: 600000
  });
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
  return request<MissionControlSnapshot>("/paper/mission-control", { timeoutMs: 60000, revalidateSeconds: 15 });
}

export function getStrategyDiagnosticsSummary() {
  return request<StrategyDiagnosticsSummary>("/strategy-diagnostics/summary", { timeoutMs: 15000, revalidateSeconds: 60 });
}

export function getEliteDeploymentAudit() {
  return request<EliteDeploymentAudit>("/elite-deployments/audit", { timeoutMs: 15000, revalidateSeconds: 60 });
}

export function getPortfolioReadiness() {
  return request<PortfolioReadiness>("/portfolio/readiness", { timeoutMs: 15000 });
}

export function getDeploymentManagement() {
  return request<DeploymentManagementSnapshot>("/paper/deployment-management", { timeoutMs: 60000, revalidateSeconds: 15 });
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

export type IntradayPaperLabDecision = {
  id: number;
  experiment_id?: number;
  experiment_name?: string;
  factor_key?: string;
  created_at: string;
  symbol: string;
  action: "enter" | "skip" | "exit" | "flatten" | "error";
  side?: "buy" | "sell" | null;
  signed_trade_imbalance?: string | number | null;
  trade_count?: number | null;
  reason?: string | null;
  broker_status?: string | null;
  client_order_id?: string | null;
};

export type IntradayPaperLabTrade = {
  id: number;
  experiment_id?: number;
  experiment_name?: string;
  factor_key?: string;
  symbol: string;
  side: "long" | "short";
  quantity: string | number;
  status: "open" | "closing" | "closed" | "rejected";
  signal_bar_start: string;
  exit_due_at: string;
  opened_at: string;
  closed_at?: string | null;
  entry_client_order_id?: string | null;
  exit_client_order_id?: string | null;
  entry_status?: string | null;
  entry_price?: string | number | null;
  entry_filled_quantity?: string | number | null;
  entry_filled_at?: string | null;
  exit_status?: string | null;
  exit_price?: string | number | null;
  exit_filled_quantity?: string | number | null;
  exit_filled_at?: string | null;
  realized_pnl?: string | number | null;
};

export type IntradayPaperLabOrder = {
  experiment_id?: number;
  experiment_name?: string;
  factor_key?: string;
  symbol: string;
  side: "buy" | "sell";
  order_type: string;
  requested_quantity: string | number;
  filled_quantity: string | number;
  filled_average_price?: string | number | null;
  status: string;
  submitted_at?: string | null;
  filled_at?: string | null;
  canceled_at?: string | null;
  expired_at?: string | null;
  client_order_id: string;
  updated_at: string;
};

export type IntradayPaperLabMonitor = {
  experiment: Record<string, any>;
  experiments?: Array<Record<string, any>>;
  summary: {
    decisions: number;
    entries_submitted: number;
    exits_submitted: number;
    skips: number;
    errors: number;
    last_decision_at?: string | null;
  };
  positions: Array<Record<string, any>>;
  recent_decisions: IntradayPaperLabDecision[];
  trades: IntradayPaperLabTrade[];
  orders: IntradayPaperLabOrder[];
  pnl: {
    realized_pnl: string | number;
    realized_trades: number;
    open_trades: number;
    awaiting_broker_sync_items: number;
  };
  broker_sync: Record<string, any>;
  market_data_feed?: string;
  market_data_note?: string;
};

export function getIntradayPaperLabMonitor(experimentId = 1) {
  return request<IntradayPaperLabMonitor>(`/intraday-paper-lab/experiments/${experimentId}`, {
    cache: "no-store",
    timeoutMs: 15000
  });
}

export function getIntradayPaperLabOverview() {
  return request<IntradayPaperLabMonitor>("/intraday-paper-lab/experiments", {
    cache: "no-store",
    timeoutMs: 15000
  });
}
