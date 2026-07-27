import { describe, expect, it } from "vitest";

import { configurationWithProfile, type ElitePortfolioConfiguration, type ElitePortfolioProfile } from "./api";

const STRICT_DEFAULTS = {
  maximum_portfolio_size: 20,
  minimum_portfolio_size: 5,
  minimum_unique_assets: 5,
  minimum_families: 4,
  minimum_timeframes: 2,
  maximum_per_symbol: 2,
  maximum_per_family: 2,
  maximum_parameter_similarity: 0.9,
  maximum_signal_correlation: 0.9,
  maximum_strategy_return_correlation: 0.75,
  minimum_correlation_observations: 30,
  timeframe_cap_numerator: 1,
  timeframe_cap_denominator: 2
};

const SINGLE_ELITE_TEST: ElitePortfolioProfile = {
  id: "single_elite_test",
  label: "Single Elite Test",
  summary: "One validated elite, deployed alone.",
  intended_use: "Controlled Alpaca paper testing.",
  diversified: false,
  warning: "A single-member portfolio has no diversification of any kind.",
  constraints: {
    minimum_portfolio_size: 1,
    maximum_portfolio_size: 1,
    minimum_unique_assets: 1,
    minimum_families: 1,
    minimum_timeframes: 1,
    maximum_per_symbol: 1,
    maximum_per_family: 1,
    timeframe_cap_numerator: 1,
    timeframe_cap_denominator: 1
  },
  resolved_constraints: { ...STRICT_DEFAULTS, ...{
    minimum_portfolio_size: 1,
    maximum_portfolio_size: 1,
    minimum_unique_assets: 1,
    minimum_families: 1,
    minimum_timeframes: 1,
    maximum_per_symbol: 1,
    maximum_per_family: 1,
    timeframe_cap_numerator: 1,
    timeframe_cap_denominator: 1
  } }
};

function strictConfiguration(): ElitePortfolioConfiguration {
  return {
    profile: "strict_diversified",
    universe: [],
    families: [],
    directions: ["long", "short"],
    timeframes: ["15m", "30m"],
    thresholds: { minimum_profit_factor: 1.2 },
    constraints: { ...STRICT_DEFAULTS },
    objective: "balanced",
    custom_size: null
  };
}

describe("configurationWithProfile", () => {
  it("replaces the constraint values, not just the profile name", () => {
    // The backend merges explicit `constraints` over the profile preset, so
    // carrying the strict defaults alongside a Single Elite Test profile
    // silently builds a strict portfolio. This is the regression that made
    // "Single Elite Test is feasible" produce no portfolio at all.
    const applied = configurationWithProfile(strictConfiguration(), SINGLE_ELITE_TEST);

    expect(applied.profile).toBe("single_elite_test");
    expect(applied.constraints).toMatchObject({
      minimum_portfolio_size: 1,
      maximum_portfolio_size: 1,
      minimum_unique_assets: 1,
      minimum_families: 1,
      minimum_timeframes: 1,
      maximum_per_symbol: 1,
      maximum_per_family: 1,
      timeframe_cap_numerator: 1,
      timeframe_cap_denominator: 1
    });
  });

  it("carries none of the strict diversity values through", () => {
    const applied = configurationWithProfile(strictConfiguration(), SINGLE_ELITE_TEST);

    expect(applied.constraints.minimum_unique_assets).not.toBe(STRICT_DEFAULTS.minimum_unique_assets);
    expect(applied.constraints.minimum_families).not.toBe(STRICT_DEFAULTS.minimum_families);
    expect(applied.constraints.minimum_portfolio_size).not.toBe(STRICT_DEFAULTS.minimum_portfolio_size);
  });

  it("keeps the protected correlation and similarity limits at their strict values", () => {
    const applied = configurationWithProfile(strictConfiguration(), SINGLE_ELITE_TEST);

    expect(applied.constraints.maximum_parameter_similarity).toBe(STRICT_DEFAULTS.maximum_parameter_similarity);
    expect(applied.constraints.maximum_signal_correlation).toBe(STRICT_DEFAULTS.maximum_signal_correlation);
    expect(applied.constraints.maximum_strategy_return_correlation).toBe(STRICT_DEFAULTS.maximum_strategy_return_correlation);
    expect(applied.constraints.minimum_correlation_observations).toBe(STRICT_DEFAULTS.minimum_correlation_observations);
  });

  it("preserves the rest of the configuration", () => {
    const original = strictConfiguration();
    const applied = configurationWithProfile(original, SINGLE_ELITE_TEST);

    expect(applied.timeframes).toEqual(original.timeframes);
    expect(applied.thresholds).toEqual(original.thresholds);
    expect(applied.objective).toBe(original.objective);
  });

  it("does not mutate the configuration it was given", () => {
    const original = strictConfiguration();
    configurationWithProfile(original, SINGLE_ELITE_TEST);

    expect(original.profile).toBe("strict_diversified");
    expect(original.constraints.minimum_unique_assets).toBe(5);
  });
});
