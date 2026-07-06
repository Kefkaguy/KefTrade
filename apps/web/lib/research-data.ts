export const assets = [
  { symbol: "BTCUSDT", className: "Crypto dev", exchange: "Binance", currency: "USDT", status: "Validated data" },
  { symbol: "ETHUSDT", className: "Crypto dev", exchange: "Binance", currency: "USDT", status: "Validated data" },
  { symbol: "SPY", className: "US ETF", exchange: "NYSE Arca", currency: "USD", status: "Research ready" },
  { symbol: "QQQ", className: "US ETF", exchange: "Nasdaq", currency: "USD", status: "Research ready" },
  { symbol: "AAPL", className: "US Equity", exchange: "Nasdaq", currency: "USD", status: "Research ready" },
  { symbol: "MSFT", className: "US Equity", exchange: "Nasdaq", currency: "USD", status: "Research ready" },
  { symbol: "NVDA", className: "US Equity", exchange: "Nasdaq", currency: "USD", status: "Research ready" },
  { symbol: "TSLA", className: "US Equity", exchange: "Nasdaq", currency: "USD", status: "Research ready" }
];

export const strategies = [
  { name: "trend_pullback", version: "v1", recommendation: "Reject", trades: 42, failure: "Insufficient stability across regimes" },
  { name: "breakout", version: "v1", recommendation: "Reject", trades: 38, failure: "Profit factor and drawdown gates failed" },
  { name: "mean_reversion", version: "v1", recommendation: "Reject", trades: 57, failure: "Losses concentrated in trending regimes" },
  { name: "momentum", version: "v1", recommendation: "Research More", trades: 64, failure: "Needs stronger cross-asset evidence" },
  { name: "volatility_breakout", version: "v1", recommendation: "Reject", trades: 31, failure: "Trade count and confidence interval failed" },
  { name: "trend_following_200ema", version: "v1", recommendation: "Reject", trades: 26, failure: "Late entries and unstable yearly returns" }
];

export const journalEntries = [
  {
    date: "2026-07-06",
    title: "Equity validation rejected best candidate",
    body: "Validation failed minimum evidence gates. Profit factor, stability, confidence interval, and trade count remain the dominant blockers.",
    status: "Reject"
  },
  {
    date: "2026-07-05",
    title: "Groq copilot verified",
    body: "Research questions were answered from stored evidence, while trading-action questions were refused before inference.",
    status: "Validated"
  },
  {
    date: "2026-07-04",
    title: "Research intelligence identified repeated stability failures",
    body: "The knowledge engine found weak cross-regime durability and recommended hypothesis tests around volatility and trend filters.",
    status: "Research More"
  },
  {
    date: "2026-07-03",
    title: "Alpha discovery produced no validated edge",
    body: "Candidate search worked correctly by rejecting weak strategies instead of promoting false positives.",
    status: "Reject"
  }
];

export const hypotheses = [
  "Momentum strategies perform better after volatility compression.",
  "Trend pullback entries improve when ATR is expanding.",
  "Mean reversion is hostile during high trend strength regimes.",
  "Breakouts require relative volume confirmation to survive equity validation."
];

export const equityCurve = [100, 101, 100.5, 102, 101.8, 103.4, 102.6, 104.8, 104, 105.5, 104.1, 106.2, 105.8, 107.4, 106.1, 108.3];
export const drawdownCurve = [0, -0.4, -1.2, -0.2, -0.8, 0, -1.4, -0.3, -1.8, -0.6, -2.6, -0.9, -1.4, -0.5, -2.1, -0.7];

export const regimeRows = [
  { label: "Bull trend", value: 72, meta: "least hostile" },
  { label: "Bear trend", value: 41, meta: "drawdown risk" },
  { label: "Sideways", value: 28, meta: "most failures" },
  { label: "High volatility", value: 55, meta: "mixed" },
  { label: "Low volatility", value: 33, meta: "low edge" }
];
