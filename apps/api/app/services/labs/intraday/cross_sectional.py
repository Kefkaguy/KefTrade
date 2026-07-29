"""Cross-sectional relative-strength ranking -- Phase 13.10 follow-up.

Every Strategy Engine V2 family tested so far judges one symbol against its
own history (a fixed relative-volume threshold, an ATR-scaled range, etc.).
Cross-sectional momentum is structurally different and one of the most
replicated findings in market research (Jegadeesh & Titman, 1993): rank
symbols against EACH OTHER at each point in time and trade the relatively
strongest / weakest, rather than testing any one symbol in isolation against
a fixed bar.

This module computes that ranking as a plain, leak-free feature value --
`cross_sectional_momentum_percentile` -- so the family that consumes it
(`CrossSectionalMomentumV2`) can be an ordinary `V2Strategy` subclass with no
change to the shared single-symbol simulator, feature engine, or gate. The
only new piece is a dataset loader (`cross_sectional_dataset.py`) that
computes this ranking across a campaign's full symbol universe once, then
attaches it to each symbol's own feature stream before the existing
single-symbol pipeline runs unmodified.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

CROSS_SECTIONAL_MOMENTUM_VERSION = "cross_sectional_momentum_v1"

# Ranking a symbol against fewer than this many peers at a given timestamp
# is not a meaningful cross-section -- reported as unmeasurable (None)
# rather than as a false extreme percentile.
MINIMUM_PEERS_FOR_RANKING = 3


def compute_cross_sectional_percentiles(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    lookback_bars: int = 8,
) -> dict[str, dict[datetime, float]]:
    """For every (symbol, timestamp) pair, the symbol's trailing
    `lookback_bars`-bar return ranked against every other symbol's trailing
    return AT THE SAME TIMESTAMP -- never against a different point in time.

    No lookahead: a symbol's trailing return at bar i uses only
    close[i - lookback_bars] and close[i], both already known at bar i.
    Percentile is 0 (weakest) to 1 (strongest); ties share the average rank
    of the tied group, so a repeated value never arbitrarily favors one
    symbol over another with the identical return.

    Timestamps where fewer than MINIMUM_PEERS_FOR_RANKING symbols have a
    computable trailing return are omitted entirely for every symbol at
    that timestamp -- an honestly unmeasurable ranking, not a fabricated
    one against too few peers to mean anything.
    """
    returns_by_timestamp: dict[datetime, dict[str, float]] = {}
    for symbol, candles in candles_by_symbol.items():
        sorted_candles = sorted(candles, key=lambda row: row["timestamp"])
        for index in range(lookback_bars, len(sorted_candles)):
            current = sorted_candles[index]
            past = sorted_candles[index - lookback_bars]
            past_close = float(past["close"])
            if past_close == 0:
                continue
            trailing_return = (float(current["close"]) - past_close) / past_close
            timestamp = current["timestamp"]
            returns_by_timestamp.setdefault(timestamp, {})[symbol] = trailing_return

    percentiles: dict[str, dict[datetime, float]] = {symbol: {} for symbol in candles_by_symbol}
    for timestamp, returns_by_symbol in returns_by_timestamp.items():
        if len(returns_by_symbol) < MINIMUM_PEERS_FOR_RANKING:
            continue
        ordered = sorted(returns_by_symbol.items(), key=lambda item: item[1])
        n = len(ordered)
        # Average-rank tie handling: a run of equal returns all receive the
        # mean of the rank positions they jointly occupy.
        rank_by_symbol: dict[str, float] = {}
        i = 0
        while i < n:
            j = i
            while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
                j += 1
            average_rank = (i + j) / 2
            for k in range(i, j + 1):
                rank_by_symbol[ordered[k][0]] = average_rank
            i = j + 1
        for symbol, rank in rank_by_symbol.items():
            percentile = rank / (n - 1) if n > 1 else 0.5
            percentiles[symbol][timestamp] = percentile

    return percentiles


def merge_percentiles_into_features(
    features: list[dict[str, Any]],
    percentiles_for_symbol: dict[datetime, float],
) -> list[dict[str, Any]]:
    """Attach `cross_sectional_momentum_percentile` to each feature row by
    timestamp. A timestamp with no ranking available gets None, not a
    fabricated neutral value -- the strategy must treat that explicitly as
    unmeasurable (see CrossSectionalMomentumV2.evaluate)."""
    merged = []
    for row in features:
        enriched = dict(row)
        enriched["cross_sectional_momentum_percentile"] = percentiles_for_symbol.get(row["timestamp"])
        merged.append(enriched)
    return merged


def compute_next_same_slot_percentiles(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    lookback_sessions: int = 20,
) -> dict[str, dict[datetime, dict[str, float]]]:
    """Rank each symbol's historical mean for the *next* intraday slot.

    The value is keyed to the current signal bar. Its score uses only earlier
    sessions at the next bar's wall-clock slot; the current next-bar return is
    therefore not part of its own predictor.
    """
    scores_by_time: dict[datetime, dict[str, float]] = {}
    for symbol, raw_rows in candles_by_symbol.items():
        rows = sorted(raw_rows, key=lambda row: row["timestamp"])
        history: dict[tuple[int, int], list[float]] = {}
        for index, row in enumerate(rows):
            timestamp = row["timestamp"]
            slot = (timestamp.hour, timestamp.minute)
            open_price = float(row["open"])
            if open_price > 0:
                history.setdefault(slot, []).append(
                    (float(row["close"]) - open_price) / open_price
                )
            if index + 1 >= len(rows):
                continue
            next_row = rows[index + 1]
            if next_row["timestamp"].date() != timestamp.date():
                continue
            next_timestamp = next_row["timestamp"]
            next_slot = (next_timestamp.hour, next_timestamp.minute)
            prior = history.get(next_slot, [])[-lookback_sessions:]
            if len(prior) < 5:
                continue
            scores_by_time.setdefault(timestamp, {})[symbol] = sum(prior) / len(prior)

    result: dict[str, dict[datetime, dict[str, float]]] = {
        symbol: {} for symbol in candles_by_symbol
    }
    for timestamp, scores in scores_by_time.items():
        if len(scores) < MINIMUM_PEERS_FOR_RANKING:
            continue
        ordered = sorted(scores.items(), key=lambda item: item[1])
        count = len(ordered)
        index = 0
        while index < count:
            end = index
            while end + 1 < count and ordered[end + 1][1] == ordered[index][1]:
                end += 1
            rank = (index + end) / 2
            for position in range(index, end + 1):
                symbol, score = ordered[position]
                result[symbol][timestamp] = {
                    "score": score,
                    "percentile": rank / (count - 1) if count > 1 else 0.5,
                }
            index = end + 1
    return result
