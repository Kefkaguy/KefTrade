export type ResearchAssetId = string;

export type ResearchAsset = {
  id: ResearchAssetId;
  apiSymbol: string;
  name: string;
  market: "Equity" | "ETF" | "Crypto";
  exchange?: string;
};

export type ResearchScopeId = "single" | "core" | "technology" | "crypto" | "index" | "universe" | "custom";

export type ResearchScope = {
  id: Exclude<ResearchScopeId, "custom">;
  label: string;
  description: string;
  assets: ResearchAssetId[];
};

export type ResearchSelection = {
  scopeId: ResearchScopeId;
  scopeLabel: string;
  assets: ResearchAsset[];
  candidateCount: number;
  estimatedJobs: number;
};

export const MIN_TARGETED_CANDIDATES = 60;
export const MAX_TARGETED_CANDIDATES = 250;
export const MAX_PROFILE_ASSETS = 100;

export const FALLBACK_RESEARCH_ASSETS: ResearchAsset[] = [
  { id: "TSLA", apiSymbol: "TSLA", name: "Tesla", market: "Equity", exchange: "NASDAQ" },
  { id: "NVDA", apiSymbol: "NVDA", name: "NVIDIA", market: "Equity", exchange: "NASDAQ" },
  { id: "AAPL", apiSymbol: "AAPL", name: "Apple", market: "Equity", exchange: "NASDAQ" },
  { id: "MSFT", apiSymbol: "MSFT", name: "Microsoft", market: "Equity", exchange: "NASDAQ" },
  { id: "AMD", apiSymbol: "AMD", name: "AMD", market: "Equity", exchange: "NASDAQ" },
  { id: "META", apiSymbol: "META", name: "Meta", market: "Equity", exchange: "NASDAQ" },
  { id: "GOOGL", apiSymbol: "GOOGL", name: "Alphabet", market: "Equity", exchange: "NASDAQ" },
  { id: "AMZN", apiSymbol: "AMZN", name: "Amazon", market: "Equity", exchange: "NASDAQ" },
  { id: "SPY", apiSymbol: "SPY", name: "S&P 500 ETF", market: "ETF", exchange: "NYSEARCA" },
  { id: "QQQ", apiSymbol: "QQQ", name: "Nasdaq 100 ETF", market: "ETF", exchange: "NASDAQ" },
  { id: "BTC", apiSymbol: "BTCUSDT", name: "Bitcoin", market: "Crypto", exchange: "BINANCE" },
  { id: "ETH", apiSymbol: "ETHUSDT", name: "Ethereum", market: "Crypto", exchange: "BINANCE" }
];

export const CRYPTO_RESEARCH_ASSETS = FALLBACK_RESEARCH_ASSETS.filter((asset) => asset.market === "Crypto");

export const RESEARCH_SCOPES: ResearchScope[] = [
  {
    id: "single",
    label: "Single Asset",
    description: "Build one versioned asset profile and specialist hypothesis.",
    assets: ["TSLA"]
  },
  {
    id: "core",
    label: "Research Core",
    description: "Profile liquid technology leaders and market ETFs.",
    assets: ["TSLA", "NVDA", "AAPL", "MSFT", "AMD", "META", "GOOGL", "AMZN", "SPY", "QQQ"]
  },
  {
    id: "technology",
    label: "Technology Stocks",
    description: "Measure and compare behavior across four liquid leaders.",
    assets: ["TSLA", "NVDA", "AAPL", "MSFT"]
  },
  {
    id: "crypto",
    label: "Crypto",
    description: "Search Bitcoin and Ethereum together.",
    assets: ["BTC", "ETH"]
  },
  {
    id: "index",
    label: "Index ETFs",
    description: "Research broad-market behavior through SPY.",
    assets: ["SPY"]
  },
  {
    id: "universe",
    label: "Measured Universe",
    description: "Use up to 100 prioritized active equities from the connected catalog.",
    assets: []
  }
];

export const RESEARCH_TIMEFRAMES = ["1h", "4h"] as const;

export const STRATEGY_FAMILIES = ["Momentum", "Pullback", "Breakout", "Mean Reversion"] as const;

export const VALIDATION_METHODS = ["Walk Forward", "Cross Asset", "Robustness", "Forward Validation"] as const;

export function buildResearchSelection(scopeId: ResearchScopeId, assets: ResearchAsset[]): ResearchSelection {
  const scope = RESEARCH_SCOPES.find((item) => item.id === scopeId);
  const candidateCount = candidateCountForAssets(assets.length);

  return {
    scopeId,
    scopeLabel: scope?.label ?? `${assets.length.toLocaleString()} selected assets`,
    assets,
    candidateCount,
    estimatedJobs: assets.length * RESEARCH_TIMEFRAMES.length * candidateCount
  };
}

export function candidateCountForAssets(assetCount: number) {
  if (assetCount <= 0) return 0;
  const focusedBudget = assetCount * 30;
  return Math.max(MIN_TARGETED_CANDIDATES, Math.min(MAX_TARGETED_CANDIDATES, focusedBudget));
}

export function researchUniverseKey(selection: ResearchSelection) {
  const symbols = selection.assets.map((asset) => asset.apiSymbol).sort().join("|");
  return `home_${selection.scopeId}_${selection.assets.length}_${stableHash(symbols)}`;
}

function stableHash(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}
