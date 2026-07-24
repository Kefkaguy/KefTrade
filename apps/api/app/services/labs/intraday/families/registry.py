"""Phase 12.3: the one place every Intraday Lab family is aggregated.

`strategy_discovery.make_strategy_definition` and
`labs/intraday/campaign.is_intraday_lab_candidate` both key off
`INTRADAY_STRATEGY_FACTORIES` from here (not from `labs/intraday/strategy.py`
directly) so that adding a new family means adding one entry to
`FAMILY_REGISTRY` below, never touching either of those functions or any
existing family's own module -- including the two archived ones,
Opening-Range Breakout v1 and VWAP Reversion v1, whose factories/blocks are
imported unchanged from `labs/intraday/strategy.py` and `labs/intraday/campaign.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.services.labs.intraday.campaign import (
    OPENING_RANGE_BREAKOUT_ARCHITECTURE,
    ORB_BLOCKS,
    SUPPORTED_ORB_TIMEFRAMES,
    SUPPORTED_VWAP_REVERSION_TIMEFRAMES,
    VWAP_REVERSION_ARCHITECTURE,
    VWAP_REVERSION_BLOCKS,
    generate_orb_candidates,
    generate_vwap_reversion_candidates,
)
from app.services.labs.intraday.strategy import INTRADAY_STRATEGY_FACTORIES as _ARCHIVED_FACTORIES
from app.services.labs.intraday.families.ema_trend_continuation import (
    EMA_TREND_CONTINUATION_ARCHITECTURE,
    EMA_TREND_CONTINUATION_BLOCKS,
    EmaTrendContinuationStrategy,
    generate_ema_trend_continuation_candidates,
)
from app.services.labs.intraday.families.gap_fill import (
    GAP_FILL_ARCHITECTURE,
    GAP_FILL_BLOCKS,
    GapFillStrategy,
    generate_gap_fill_candidates,
)
from app.services.labs.intraday.families.intraday_trend_pullback import (
    INTRADAY_TREND_PULLBACK_ARCHITECTURE,
    INTRADAY_TREND_PULLBACK_BLOCKS,
    IntradayTrendPullbackStrategy,
    generate_intraday_trend_pullback_candidates,
)
from app.services.labs.intraday.families.opening_fade import (
    OPENING_FADE_ARCHITECTURE,
    OPENING_FADE_BLOCKS,
    OpeningFadeStrategy,
    generate_opening_fade_candidates,
)
from app.services.labs.intraday.families.session_momentum import (
    SESSION_MOMENTUM_ARCHITECTURE,
    SESSION_MOMENTUM_BLOCKS,
    SessionMomentumStrategy,
    generate_session_momentum_candidates,
)
from app.services.labs.intraday.families.vwap_trend_continuation import (
    VWAP_TREND_CONTINUATION_ARCHITECTURE,
    VWAP_TREND_CONTINUATION_BLOCKS,
    VwapTrendContinuationStrategy,
    generate_vwap_trend_continuation_candidates,
)

SUPPORTED_INTRADAY_TIMEFRAMES = ("15m", "30m")


@dataclass(frozen=True)
class IntradayFamilyDefinition:
    architecture: str
    name: str
    strategy_cls: type
    blocks: dict[str, str]
    candidate_generator: Callable[..., list[Any]]
    supported_timeframes: tuple[str, ...]
    status: str  # "archived" | "active"


FAMILY_REGISTRY: dict[str, IntradayFamilyDefinition] = {
    OPENING_RANGE_BREAKOUT_ARCHITECTURE: IntradayFamilyDefinition(
        architecture=OPENING_RANGE_BREAKOUT_ARCHITECTURE,
        name="Opening Range Breakout",
        strategy_cls=_ARCHIVED_FACTORIES[OPENING_RANGE_BREAKOUT_ARCHITECTURE],
        blocks=ORB_BLOCKS,
        candidate_generator=generate_orb_candidates,
        supported_timeframes=SUPPORTED_ORB_TIMEFRAMES,
        status="archived",
    ),
    VWAP_REVERSION_ARCHITECTURE: IntradayFamilyDefinition(
        architecture=VWAP_REVERSION_ARCHITECTURE,
        name="VWAP Reversion",
        strategy_cls=_ARCHIVED_FACTORIES[VWAP_REVERSION_ARCHITECTURE],
        blocks=VWAP_REVERSION_BLOCKS,
        candidate_generator=generate_vwap_reversion_candidates,
        supported_timeframes=SUPPORTED_VWAP_REVERSION_TIMEFRAMES,
        status="archived",
    ),
    GAP_FILL_ARCHITECTURE: IntradayFamilyDefinition(
        architecture=GAP_FILL_ARCHITECTURE,
        name="Gap Fill",
        strategy_cls=GapFillStrategy,
        blocks=GAP_FILL_BLOCKS,
        candidate_generator=generate_gap_fill_candidates,
        supported_timeframes=SUPPORTED_INTRADAY_TIMEFRAMES,
        status="active",
    ),
    SESSION_MOMENTUM_ARCHITECTURE: IntradayFamilyDefinition(
        architecture=SESSION_MOMENTUM_ARCHITECTURE,
        name="Session Momentum",
        strategy_cls=SessionMomentumStrategy,
        blocks=SESSION_MOMENTUM_BLOCKS,
        candidate_generator=generate_session_momentum_candidates,
        supported_timeframes=SUPPORTED_INTRADAY_TIMEFRAMES,
        status="active",
    ),
    INTRADAY_TREND_PULLBACK_ARCHITECTURE: IntradayFamilyDefinition(
        architecture=INTRADAY_TREND_PULLBACK_ARCHITECTURE,
        name="Intraday Trend Pullback",
        strategy_cls=IntradayTrendPullbackStrategy,
        blocks=INTRADAY_TREND_PULLBACK_BLOCKS,
        candidate_generator=generate_intraday_trend_pullback_candidates,
        supported_timeframes=SUPPORTED_INTRADAY_TIMEFRAMES,
        status="active",
    ),
    EMA_TREND_CONTINUATION_ARCHITECTURE: IntradayFamilyDefinition(
        architecture=EMA_TREND_CONTINUATION_ARCHITECTURE,
        name="EMA Trend Continuation",
        strategy_cls=EmaTrendContinuationStrategy,
        blocks=EMA_TREND_CONTINUATION_BLOCKS,
        candidate_generator=generate_ema_trend_continuation_candidates,
        supported_timeframes=SUPPORTED_INTRADAY_TIMEFRAMES,
        status="active",
    ),
    OPENING_FADE_ARCHITECTURE: IntradayFamilyDefinition(
        architecture=OPENING_FADE_ARCHITECTURE,
        name="Opening Fade",
        strategy_cls=OpeningFadeStrategy,
        blocks=OPENING_FADE_BLOCKS,
        candidate_generator=generate_opening_fade_candidates,
        supported_timeframes=SUPPORTED_INTRADAY_TIMEFRAMES,
        status="active",
    ),
    VWAP_TREND_CONTINUATION_ARCHITECTURE: IntradayFamilyDefinition(
        architecture=VWAP_TREND_CONTINUATION_ARCHITECTURE,
        name="VWAP Trend Continuation",
        strategy_cls=VwapTrendContinuationStrategy,
        blocks=VWAP_TREND_CONTINUATION_BLOCKS,
        candidate_generator=generate_vwap_trend_continuation_candidates,
        supported_timeframes=SUPPORTED_INTRADAY_TIMEFRAMES,
        status="active",
    ),
}

# Every consumer that needs "architecture marker -> strategy class" (the
# simulator-facing factory dispatch in `make_strategy_definition`) reads
# this, not FAMILY_REGISTRY directly, so it stays a plain, minimal dict.
INTRADAY_STRATEGY_FACTORIES: dict[str, type] = {
    architecture: definition.strategy_cls for architecture, definition in FAMILY_REGISTRY.items()
}


def create_intraday_campaign(
    conn: Any,
    *,
    family_ids: list[str],
    name: str | None = None,
    asset_limit: int = 10,
    timeframes: list[str] | None = None,
    max_candidates_per_family: int = 8,
    campaign_label: str | None = None,
    hypothesis_version_id: int | None = None,
) -> dict[str, Any]:
    """Launch a campaign against one, several, or all Intraday Lab families.

    Every family's candidates keep their own `strategy_architecture` field
    (set by that family's own candidate generator) all the way through --
    `run_campaign_job`/`make_strategy_definition` dispatch each job to its
    own strategy class, and each job is evaluated by the unmodified elite
    gate independently. Nothing here merges or blends evidence across
    families; a multi-family campaign is simply the union of each
    requested family's own candidate list, queued together.
    """
    from app.services.labs.intraday.campaign import _create_intraday_campaign

    if not family_ids:
        raise ValueError("at least one family_id is required")
    unknown = [family_id for family_id in family_ids if family_id not in FAMILY_REGISTRY]
    if unknown:
        raise ValueError(f"Unknown intraday family id(s): {unknown}. Available: {sorted(FAMILY_REGISTRY)}")

    definitions = [FAMILY_REGISTRY[family_id] for family_id in family_ids]
    all_candidates: list[Any] = []
    for definition in definitions:
        all_candidates.extend(definition.candidate_generator(max_candidates=max_candidates_per_family))

    combined_supported_timeframes = tuple(
        sorted({timeframe for definition in definitions for timeframe in definition.supported_timeframes})
    )
    multi_key = ",".join(sorted(family_id for family_id in family_ids))
    family_names = ", ".join(definition.name for definition in definitions)

    return _create_intraday_campaign(
        conn,
        name=name or f"Intraday Lab pilot: {family_names}",
        architecture=f"multi_family:{multi_key}",
        strategy_family_label=family_names,
        candidates=all_candidates,
        supported_timeframes=combined_supported_timeframes,
        timeframes=timeframes,
        asset_limit=asset_limit,
        campaign_label=campaign_label,
        hypothesis_version_id=hypothesis_version_id,
    )


def is_intraday_lab_candidate(candidate_payload: dict[str, Any]) -> bool:
    """True when a raw (JSONB) candidate payload belongs to any Intraday Lab family.

    Used at the two points that need to route intraday jobs differently
    (`run_campaign_job`'s dataset selection, `data_readiness_for_job`'s
    feature-coverage check) without branching on strategy identity anywhere
    inside the simulator itself. Keyed off `FAMILY_REGISTRY`, so a new
    family registering there is automatically covered here too.
    """
    parameters = candidate_payload.get("parameters") or {}
    return parameters.get("strategy_architecture") in FAMILY_REGISTRY
