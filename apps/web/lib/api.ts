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

export function getRiskSettings() {
  return request<RiskSettings>("/risk/settings");
}

export function updateRiskSettings(payload: Partial<RiskSettings>) {
  return request<RiskSettings>("/risk/settings", { method: "PUT", body: JSON.stringify(payload) });
}
