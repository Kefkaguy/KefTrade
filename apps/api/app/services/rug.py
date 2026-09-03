from __future__ import annotations

"""RUG: reproducible broad candidate generation for the existing research judge.

RUG deliberately does not score, promote, or reject a strategy.  It only builds
executable ``DiscoveryCandidate`` objects.  Campaign workers, the backtester,
and the unchanged validation gates remain the sole judges.
"""

import math
import random
from dataclasses import replace
from hashlib import sha256
from typing import Any

from app.services.research_learning import score_candidate_for_guidance
from app.services.strategy import BASE_PARAMETERS
from app.services.strategy_discovery import (
    DiscoveryCandidate,
    candidate_execution_key,
    canonical_candidate_key,
)

RUG_VERSION = "rug_v1"
RUG_DEFAULT_ALLOCATION = {
    "evidence_exploitation": 0.60,
    "random_exploration": 0.30,
    "challenge": 0.10,
}

FAST_PERIODS = (5, 7, 8, 9, 10, 12, 15, 20, 30, 50)
SLOW_PERIODS = (20, 30, 40, 50, 75, 100, 150, 200)
RSI_PERIODS = (7, 8, 9, 10, 12, 14, 16, 20, 21)
RSI_LONG_THRESHOLDS = (48, 50, 52, 55, 58, 60, 62, 65)
RSI_OVERSOLD_THRESHOLDS = (25, 28, 30, 32, 35, 38, 40, 42, 45)
RISK_REWARDS = (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)
ATR_MULTIPLIERS = (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
HOLDING_BARS = (4, 6, 8, 10, 12, 16, 20, 30, 40)
ENTRY_WINDOWS_UTC = ((0, 1440), (570, 660), (570, 720), (600, 780), (780, 960), (900, 1200))
VOLATILITY_MINIMUMS = (0.0, 0.004, 0.006, 0.008, 0.01, 0.015, 0.02)
VOLUME_MINIMUMS = (-0.30, -0.15, 0.0, 0.10, 0.20, 0.35)


def rug_channel_counts(total: int, *, guidance_available: bool) -> dict[str, int]:
    """Allocate a batch while preserving a genuinely random channel.

    Before there is learned evidence, exploitation cannot honestly exist, so
    its allocation is reassigned to exploration.  Once evidence exists the
    declared 60/30/10 split is used, with rounding resolved deterministically.
    """

    if total <= 0:
        return {key: 0 for key in RUG_DEFAULT_ALLOCATION}
    allocation = RUG_DEFAULT_ALLOCATION if guidance_available else {
        "evidence_exploitation": 0.0,
        "random_exploration": 0.90,
        "challenge": 0.10,
    }
    counts = {key: math.floor(total * share) for key, share in allocation.items()}
    remainder = total - sum(counts.values())
    order = sorted(allocation, key=lambda key: (-(total * allocation[key] - counts[key]), key))
    for key in order[:remainder]:
        counts[key] += 1
    return counts


def generate_rug_candidates(
    *,
    max_candidates: int,
    seed: int,
    batch_index: int = 0,
    guidance: dict[str, Any] | None = None,
) -> tuple[list[DiscoveryCandidate], dict[str, Any]]:
    if max_candidates <= 0:
        return [], _metrics(seed, batch_index, {}, 0, 0, bool((guidance or {}).get("available")))
    if batch_index < 0:
        raise ValueError("RUG batch_index must be non-negative")

    guidance = dict(guidance or {})
    guidance_available = bool(guidance.get("available"))
    counts = rug_channel_counts(max_candidates, guidance_available=guidance_available)
    rng = random.Random(_stream_seed(seed, batch_index))
    # A larger pool lets learning choose among broad random proposals without
    # turning the exploitation channel into a hand-authored parameter grid.
    pool_target = max(max_candidates * 5, 250)
    pool: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    attempts = 0
    attempt_limit = max(pool_target * 30, 1000)
    while len(pool) < pool_target and attempts < attempt_limit:
        attempts += 1
        candidate = _random_candidate(rng, seed=seed, batch_index=batch_index, ordinal=attempts)
        execution_key = candidate_execution_key(candidate)
        if execution_key in seen:
            continue
        seen.add(execution_key)
        pool.append(candidate)

    scored = [(score_candidate_for_guidance(candidate, guidance), rng.random(), candidate) for candidate in pool]
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    selected: list[DiscoveryCandidate] = []
    selected_keys: set[str] = set()
    selected_by_channel: dict[str, int] = {}

    def take(rows: list[tuple[float, float, DiscoveryCandidate]], count: int, channel: str) -> None:
        for _, _, candidate in rows:
            if selected_by_channel.get(channel, 0) >= count:
                break
            execution_key = candidate_execution_key(candidate)
            if execution_key in selected_keys:
                continue
            selected_keys.add(execution_key)
            selected.append(_with_channel(candidate, channel))
            selected_by_channel[channel] = selected_by_channel.get(channel, 0) + 1

    take(scored, counts["evidence_exploitation"], "evidence_exploitation")
    random_rows = list(scored)
    rng.shuffle(random_rows)
    take(random_rows, counts["random_exploration"], "random_exploration")
    # Challenge candidates deliberately sample the regions that current
    # evidence scores lowest.  They can falsify an avoid conclusion; they do
    # not receive relaxed gates or any promotion privilege.
    take(list(reversed(scored)), counts["challenge"], "challenge")

    if len(selected) < max_candidates:
        take(random_rows, max_candidates - len(selected), "random_exploration")
    selected = selected[:max_candidates]
    actual_channels: dict[str, int] = {}
    for candidate in selected:
        channel = str(candidate.parameters["rug_channel"])
        actual_channels[channel] = actual_channels.get(channel, 0) + 1
    return selected, _metrics(seed, batch_index, actual_channels, attempts, len(pool), guidance_available)


def _random_candidate(rng: random.Random, *, seed: int, batch_index: int, ordinal: int) -> DiscoveryCandidate:
    entry = rng.choice(("breakout", "pullback", "mean_reversion", "trend_continuation", "opening_range_proxy"))
    momentum = "rsi_oversold" if entry == "mean_reversion" else rng.choice(("rsi", "roc", "stochastic_proxy"))
    fast = rng.choice(FAST_PERIODS)
    valid_slow = [period for period in SLOW_PERIODS if period > fast]
    slow = rng.choice(valid_slow)
    volatility = rng.choice(("atr", "keltner", "donchian"))
    start_minute, end_minute = rng.choice(ENTRY_WINDOWS_UTC)
    rsi_period = rng.choice(RSI_PERIODS)
    rsi_threshold = rng.choice(RSI_OVERSOLD_THRESHOLDS if momentum == "rsi_oversold" else RSI_LONG_THRESHOLDS)
    risk_reward = rng.choice(RISK_REWARDS)
    atr_multiplier = rng.choice(ATR_MULTIPLIERS)
    max_holding_bars = rng.choice(HOLDING_BARS)
    params = {
        **BASE_PARAMETERS,
        "trend_method": "ema",
        "trend_fast": fast,
        "trend_slow": slow,
        "momentum": momentum,
        "rsi_period": rsi_period,
        "rsi_min": rsi_threshold if momentum != "rsi_oversold" else 45,
        "rsi_max": rng.choice((65, 70, 72, 75, 80)),
        "rsi_oversold": rsi_threshold if momentum == "rsi_oversold" else 38,
        "returns_5_min": rng.choice((0.0, 0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02)),
        "volatility": volatility,
        "volatility_20_min": rng.choice(VOLATILITY_MINIMUMS),
        "volume": "relative",
        "volume_change_min": rng.choice(VOLUME_MINIMUMS),
        "entry": entry,
        "breakout_lookback": rng.choice((4, 6, 8, 10, 12, 20, 30, 40)),
        "entry_distance_to_ema20_max": rng.choice((0.005, 0.01, 0.015, 0.02, 0.03, 0.04)),
        "exit": rng.choice(("fixed_rr", "atr_stop", "time_exit", "trailing_proxy")),
        "risk_reward": risk_reward,
        "atr_multiplier": atr_multiplier,
        "swing_lookback": rng.choice((3, 4, 5, 6, 8, 10, 12, 20)),
        "max_holding_bars": max_holding_bars,
        "entry_start_minute_utc": start_minute,
        "entry_end_minute_utc": end_minute,
        "recent_candle_window_bars": max(220, slow + 20),
        "frequency_screen_min_opportunities": 30,
        "generator_version": RUG_VERSION,
        "rug_seed": seed,
        "rug_batch_index": batch_index,
        "rug_ordinal": ordinal,
        "rug_channel": "unassigned",
    }
    blocks = {
        "trend": f"ema_{fast}_{slow}",
        "momentum": f"{momentum}_period_{rsi_period}",
        "volatility": volatility,
        "volume": "relative_volume",
        "entry": entry,
        "exit": str(params["exit"]),
    }
    canonical_key = canonical_candidate_key(blocks, params)
    digest = sha256(canonical_key.encode()).hexdigest()
    return DiscoveryCandidate(
        candidate_id=f"rug_{digest[:16]}",
        family_id=f"rug_family_{entry}",
        parent_candidate_id=None,
        generation=batch_index + 1,
        blocks=blocks,
        parameters=params,
        complexity=6,
        canonical_key=canonical_key,
    )


def _with_channel(candidate: DiscoveryCandidate, channel: str) -> DiscoveryCandidate:
    params = {**candidate.parameters, "rug_channel": channel, "generation_channel": f"rug_{channel}"}
    canonical_key = canonical_candidate_key(candidate.blocks, params)
    return replace(
        candidate,
        candidate_id=f"rug_{sha256(canonical_key.encode()).hexdigest()[:16]}",
        parameters=params,
        canonical_key=canonical_key,
    )


def _stream_seed(seed: int, batch_index: int) -> int:
    return int(sha256(f"{RUG_VERSION}|{seed}|{batch_index}".encode()).hexdigest()[:16], 16)


def _metrics(seed: int, batch_index: int, channels: dict[str, int], attempts: int, pool_size: int, guidance_available: bool) -> dict[str, Any]:
    return {
        "mode": "rug",
        "generator_version": RUG_VERSION,
        "seed": seed,
        "batch_index": batch_index,
        "channels": channels,
        "attempted_candidate_generations": attempts,
        "unique_pool_size": pool_size,
        "learning_guidance_available": guidance_available,
        "judge": "existing_backtester_and_validation_pipeline",
        "promotion_authority": False,
    }
