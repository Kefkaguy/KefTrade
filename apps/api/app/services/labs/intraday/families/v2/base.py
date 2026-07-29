"""Phase 13.3: shared machinery for Strategy Engine V2 families.

Phase 12.3 built six families as six near-duplicate modules; the duplication
is why a fix in one never reached the others. V2 instead puts every shared
concern here -- session state, entry cutoff, direction gating, feature
computation, decision construction -- so a family module contains only what
actually makes that family different: its hypothesis and its entry test.

Each family declares a `HypothesisSpec` alongside its logic. The spec is not
documentation: it is stored on every candidate, drives the hypothesis
registry rows, and supplies the human-readable explanations in Phase 13.9.
A family cannot be registered without one.

Long-first policy (Phase 13 rule): every family supports long. Shorts are
generated only where the family's hypothesis is genuinely symmetric, and
`execution_capability` stays `simulation_only` for every V2 family -- none
is eligible for external paper execution until it has passed the unchanged
elite gate and been explicitly reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Sequence

from app.services.labs.intraday.dataset import minimum_entry_lookahead_minutes
from app.services.labs.intraday.feature_engine_v2 import (
    DEFAULT_CONFIG,
    FEATURE_ENGINE_VERSION,
    compute_v2_features,
)
from app.services.strategy import ExecutionConstraints, StrategyDecision
from app.services.strategy_dna import register_family_dna

STRATEGY_ENGINE_VERSION = "strategy_engine_v2"
SUPPORTED_V2_TIMEFRAMES = ("15m", "30m")


@dataclass(frozen=True)
class HypothesisSpec:
    """The falsifiable claim a family exists to test.

    `success_criteria` is written BEFORE any campaign runs -- that ordering
    is the whole point, and it is what stops a result from being judged
    against numbers chosen after seeing it.
    """

    title: str
    market_behavior: str
    hypothesis: str
    required_conditions: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    success_criteria: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "market_behavior": self.market_behavior,
            "hypothesis": self.hypothesis,
            "required_conditions": list(self.required_conditions),
            "invalidation_conditions": list(self.invalidation_conditions),
            "success_criteria": dict(self.success_criteria),
        }


@dataclass
class EntryPlan:
    """A family's answer when it wants to trade. `take_profit` is a literal
    price for absolute-target families and is ignored (recomputed from R) for
    the rest -- see `V2Strategy.uses_absolute_targets`."""

    direction: str
    stop_loss: Decimal
    reason: str
    take_profit: Decimal | None = None


@dataclass
class SessionState:
    current_session: date | None = None
    entries_taken: int = 0
    long_taken: bool = False
    short_taken: bool = False


def _avoid(reason: str) -> StrategyDecision:
    return StrategyDecision("avoid", None, None, None, None, [reason])


class V2Strategy:
    """Base for every Strategy Engine V2 family. Satisfies `StrategyProtocol`.

    Subclasses implement `evaluate()` and set the class attributes below.
    Everything else -- session rollover, per-session entry caps, the
    session-close entry cutoff, direction eligibility, feature computation,
    stop/target construction -- is handled once, here.
    """

    architecture: str = ""
    hypothesis: HypothesisSpec
    feature_groups: tuple[str, ...] = ("session",)
    supported_timeframes: tuple[str, ...] = SUPPORTED_V2_TIMEFRAMES
    uses_absolute_targets: bool = False
    supports_short: bool = True

    def __init__(self, params: dict[str, Any], *, timeframe: str):
        if timeframe not in self.supported_timeframes:
            raise ValueError(
                f"{self.architecture}: timeframe {timeframe!r} not permitted; "
                f"allowed {self.supported_timeframes}"
            )
        direction = str(params.get("direction", "long"))
        allowed = ("long", "short", "both") if self.supports_short else ("long",)
        if direction not in allowed:
            raise ValueError(
                f"{self.architecture}: direction {direction!r} not permitted; allowed {allowed}"
            )
        self.params = params
        self.timeframe = timeframe
        self.execution_constraints = ExecutionConstraints(
            flat_by_session_close=True,
            honor_absolute_take_profit=self.uses_absolute_targets,
        )
        self.state = SessionState()

    def reset(self) -> None:
        self.state = SessionState()

    # -- subclass hook ------------------------------------------------------
    def evaluate(
        self,
        candle: dict[str, Any],
        feature: dict[str, Any],
        v2: dict[str, Any],
        params: dict[str, Any],
    ) -> EntryPlan | str:
        """Return an `EntryPlan` to trade, or a string explaining the refusal.
        The string becomes the stored rejection explanation, so it must name
        the specific condition that failed."""
        raise NotImplementedError

    # -- simulator-facing ---------------------------------------------------
    def __call__(
        self,
        candle: dict[str, Any],
        feature: dict[str, Any],
        recent_candles: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> StrategyDecision:
        session_date = feature.get("session_date")
        if session_date != self.state.current_session:
            self.state = SessionState(current_session=session_date)

        if self.state.entries_taken >= int(params.get("maximum_entries_per_session", 1)):
            return _avoid("Maximum entries for this session already reached.")

        required_minutes = max(
            minimum_entry_lookahead_minutes(self.timeframe, entry_offset_bars=1, minimum_holding_bars=1),
            int(params.get("minimum_minutes_before_close_for_entry") or 0),
        )
        minutes_to_close = feature.get("minutes_to_close")
        if minutes_to_close is None or minutes_to_close < required_minutes:
            return _avoid("Too close to session close for a safe next-bar-open entry.")

        # Feature Engine V2 is pure for a given row, bounded recent-window,
        # and feature-group set. Campaigns run many parameter variants over
        # the same immutable dataset, so recomputing these O(window) feature
        # passes for every variant turns a 5k-row dataset into tens of seconds
        # per job. Keep the cache on the mutable feature row: it is scoped to
        # the in-memory dataset cache, never persisted, and is naturally
        # discarded when the worker evicts that dataset.
        cache_key = (
            self.feature_groups,
            min(len(recent_candles), DEFAULT_CONFIG.lookback_bars) if DEFAULT_CONFIG.lookback_bars else len(recent_candles),
        )
        feature_cache = feature.setdefault("_v2_feature_cache", {})
        v2 = feature_cache.get(cache_key)
        if v2 is None:
            v2 = compute_v2_features(candle, feature, recent_candles, config=DEFAULT_CONFIG, groups=self.feature_groups)
            feature_cache[cache_key] = v2
        outcome = self.evaluate(candle, feature, v2, params)
        if isinstance(outcome, str):
            return _avoid(outcome)

        direction_setting = str(params.get("direction", "long"))
        if outcome.direction == "long" and direction_setting not in ("long", "both"):
            return _avoid("Long signal but this candidate only trades the other side.")
        if outcome.direction == "short" and direction_setting not in ("short", "both"):
            return _avoid("Short signal but this candidate only trades the other side.")
        if outcome.direction == "long" and self.state.long_taken:
            return _avoid("Long side already traded this session.")
        if outcome.direction == "short" and self.state.short_taken:
            return _avoid("Short side already traded this session.")

        close = Decimal(candle["close"])
        risk = (close - outcome.stop_loss) if outcome.direction == "long" else (outcome.stop_loss - close)
        if risk <= 0:
            return _avoid("Stop is on the wrong side of price; no valid risk distance.")

        reward_risk = Decimal(str(params.get("reward_risk_multiple", 1.5)))
        if self.uses_absolute_targets:
            take_profit = outcome.take_profit
            if take_profit is None:
                return _avoid("Absolute-target family produced no target price.")
        else:
            take_profit = (
                close + (risk * reward_risk) if outcome.direction == "long" else close - (risk * reward_risk)
            )

        self.state.entries_taken += 1
        if outcome.direction == "long":
            self.state.long_taken = True
        else:
            self.state.short_taken = True

        return StrategyDecision(
            "setup",
            (close, close),
            outcome.stop_loss,
            take_profit,
            reward_risk,
            [outcome.reason],
            direction=outcome.direction,
        )


# ---------------------------------------------------------------------------
# Registration + candidate generation
# ---------------------------------------------------------------------------

V2_FAMILIES: dict[str, type[V2Strategy]] = {}
V2_HYPOTHESES: dict[str, HypothesisSpec] = {}
V2_PARAMETER_GRIDS: dict[str, dict[str, Sequence[Any]]] = {}
V2_BLOCKS: dict[str, dict[str, str]] = {}


BASE_V2_PARAMETERS: dict[str, Any] = {
    "strategy_engine_version": STRATEGY_ENGINE_VERSION,
    "feature_engine_version": FEATURE_ENGINE_VERSION,
    "maximum_entries_per_session": 1,
    "minimum_minutes_before_close_for_entry": 0,
    "fee_rate": Decimal("0.001"),
    "slippage_rate": Decimal("0.0005"),
    "risk_per_trade": Decimal("0.01"),
    "initial_equity": Decimal("10000"),
    "walk_forward_train_ratio": 0.7,
    "max_holding_bars": 0,
    "recent_candle_window_bars": DEFAULT_CONFIG.lookback_bars,
    "reward_risk_multiple": Decimal("1.5"),
    "risk_reward": Decimal("1.5"),
    "direction": "long",
}


def register_v2_family(
    strategy_cls: type[V2Strategy],
    *,
    dna: dict[str, Any],
    parameter_grid: dict[str, Sequence[Any]],
    blocks: dict[str, str],
) -> None:
    """Register a family's class, DNA, hypothesis, grid, and block labels in
    one call, so the four can never drift apart."""

    architecture = strategy_cls.architecture
    if not architecture:
        raise ValueError(f"{strategy_cls.__name__} must set `architecture`")
    if not getattr(strategy_cls, "hypothesis", None):
        raise ValueError(f"{architecture} must declare a HypothesisSpec")
    register_family_dna(architecture, dna)
    V2_FAMILIES[architecture] = strategy_cls
    V2_HYPOTHESES[architecture] = strategy_cls.hypothesis
    V2_PARAMETER_GRIDS[architecture] = parameter_grid
    V2_BLOCKS[architecture] = blocks


def generate_v2_candidates(architecture: str, *, max_candidates: int = 8) -> list[Any]:
    """Deterministic candidate generation: the parameter grid is expanded in a
    fixed, sorted order and truncated, so the same request always yields the
    same candidates in the same order (no RNG, no set iteration)."""

    from hashlib import sha256
    from itertools import product

    from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key

    if architecture not in V2_PARAMETER_GRIDS:
        raise ValueError(f"Unknown Strategy Engine V2 family {architecture!r}")
    strategy_cls = V2_FAMILIES[architecture]
    grid = V2_PARAMETER_GRIDS[architecture]
    blocks = V2_BLOCKS[architecture]

    keys = sorted(grid)
    candidates: list[Any] = []
    seen: set[str] = set()
    for combination in product(*(list(grid[key]) for key in keys)):
        overrides = dict(zip(keys, combination))
        if not strategy_cls.supports_short and str(overrides.get("direction", "long")) != "long":
            continue
        parameters = {
            **BASE_V2_PARAMETERS,
            **overrides,
            "strategy_architecture": architecture,
        }
        canonical_key = canonical_candidate_key(blocks, parameters)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        short_name = architecture.replace("_v2", "").replace("_", "")[:12]
        candidates.append(
            DiscoveryCandidate(
                candidate_id=f"{short_name}_{sha256(canonical_key.encode()).hexdigest()[:14]}",
                family_id=architecture,
                parent_candidate_id=None,
                generation=1,
                blocks=dict(blocks),
                parameters=parameters,
                complexity=len(overrides),
                canonical_key=canonical_key,
            )
        )
        if len(candidates) >= max_candidates:
            break
    return candidates
