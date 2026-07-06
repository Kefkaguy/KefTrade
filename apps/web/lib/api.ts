const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type Candle = {
  timestamp: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
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

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3500);
  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    cache: "no-store",
    signal: controller.signal,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {})
    }
  }).finally(() => clearTimeout(timeout));
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export function getCandles(limit = 220) {
  return request<Candle[]>(`/candles/BTCUSDT?timeframe=4h&limit=${limit}`);
}

export function getSignal() {
  return request<Signal>("/signals/BTCUSDT?timeframe=4h");
}

export function generateSignal() {
  return request<Signal>("/signals/generate?symbol=BTCUSDT&timeframe=4h", { method: "POST" });
}

export function syncCandles() {
  return request<Record<string, unknown>>("/data/sync?symbol=BTCUSDT&timeframe=4h&provider=binance_dev&limit=1500", { method: "POST" });
}

export function syncFeatures() {
  return request<Record<string, unknown>>("/features/sync?symbol=BTCUSDT&timeframe=4h", { method: "POST" });
}

export function runBacktest() {
  return request<BacktestResult>("/backtests?symbol=BTCUSDT&timeframe=4h", { method: "POST" });
}

export function runStrategyResearch() {
  return request<StrategyResearchReport>("/research/strategies?symbol=BTCUSDT&timeframe=4h", { method: "POST" });
}

export function runAlphaDiscovery(maxCandidates = 250) {
  return request<AlphaDiscoveryReport>(`/alpha/discover?symbol=BTCUSDT&timeframe=4h&max_candidates=${maxCandidates}`, { method: "POST" });
}

export function runAlphaValidation(maxCandidates = 50) {
  return request<AlphaValidationReport>(`/alpha/validate?max_candidates=${maxCandidates}`, { method: "POST" });
}

export function getRiskSettings() {
  return request<RiskSettings>("/risk/settings");
}

export function updateRiskSettings(payload: Partial<RiskSettings>) {
  return request<RiskSettings>("/risk/settings", { method: "PUT", body: JSON.stringify(payload) });
}
