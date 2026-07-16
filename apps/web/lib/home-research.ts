export type ResearchAssetId = string;

export type ResearchAsset = {
  id: ResearchAssetId;
  apiSymbol: string;
  name: string;
  market: "Equity" | "ETF" | "Crypto";
  exchange?: string;
};

export type ResearchScopeId = "single" | "technology" | "crypto" | "index" | "universe" | "custom";

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

export const MIN_RESEARCH_JOBS = 10000;
export const MAX_RESEARCH_CANDIDATES = 5000;

export const FALLBACK_RESEARCH_ASSETS: ResearchAsset[] = [
  { id: "TSLA", apiSymbol: "TSLA", name: "Tesla", market: "Equity", exchange: "NASDAQ" },
  { id: "NVDA", apiSymbol: "NVDA", name: "NVIDIA", market: "Equity", exchange: "NASDAQ" },
  { id: "AAPL", apiSymbol: "AAPL", name: "Apple", market: "Equity", exchange: "NASDAQ" },
  { id: "MSFT", apiSymbol: "MSFT", name: "Microsoft", market: "Equity", exchange: "NASDAQ" },
  { id: "SPY", apiSymbol: "SPY", name: "S&P 500 ETF", market: "ETF", exchange: "NYSEARCA" },
  { id: "BTC", apiSymbol: "BTCUSDT", name: "Bitcoin", market: "Crypto", exchange: "BINANCE" },
  { id: "ETH", apiSymbol: "ETHUSDT", name: "Ethereum", market: "Crypto", exchange: "BINANCE" }
];

export const CRYPTO_RESEARCH_ASSETS = FALLBACK_RESEARCH_ASSETS.filter((asset) => asset.market === "Crypto");

export const RESEARCH_SCOPES: ResearchScope[] = [
  {
    id: "single",
    label: "Single Asset",
    description: "Focus 10,000 jobs on one market.",
    assets: ["TSLA"]
  },
  {
    id: "technology",
    label: "Technology Stocks",
    description: "Compare signals across four liquid leaders.",
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
    label: "Entire Alpaca Universe",
    description: "Use every active tradable US equity returned by Alpaca.",
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
  const required = Math.ceil(MIN_RESEARCH_JOBS / (assetCount * RESEARCH_TIMEFRAMES.length));
  return Math.max(1, Math.min(MAX_RESEARCH_CANDIDATES, required));
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
