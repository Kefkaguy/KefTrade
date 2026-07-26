from dataclasses import asdict

from app.services.labs.intraday.low_timeframe_expansion import (
    generate_low_timeframe_variants,
    low_timeframe_expansion_blueprint,
    select_low_timeframe_parent_rows,
)
from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key


def _candidate(candidate_id: str, *, family_id: str = "session_momentum_v2") -> dict:
    blocks = {
        "trend": "session_momentum_context",
        "momentum": "opening_session_momentum",
        "volatility": "session_atr_guard",
        "volume": "relative_volume_guard",
        "entry": "session_momentum_entry",
        "exit": "session_close_forced",
    }
    parameters = {
        "strategy_engine_version": "strategy_engine_v2",
        "strategy_architecture": family_id,
        "maximum_entries_per_session": 1,
        "minimum_minutes_before_close_for_entry": 0,
        "reward_risk_multiple": 1.5,
        "relative_volume_min": 1.2,
        "momentum_threshold": 0.006,
        "direction": "long",
    }
    canonical_key = canonical_candidate_key(blocks, parameters)
    return asdict(
        DiscoveryCandidate(
            candidate_id=candidate_id,
            family_id=family_id,
            parent_candidate_id=None,
            generation=1,
            blocks=blocks,
            parameters=parameters,
            complexity=6,
            canonical_key=canonical_key,
        )
    )


def _row(
    *,
    candidate_id: str,
    asset: str,
    timeframe: str,
    status: str,
    family: str,
    profit_factor: float,
    expectancy: float,
    trades: int,
    drawdown: float,
) -> dict:
    return {
        "job_id": hash((candidate_id, asset, timeframe)) % 100000,
        "campaign_id": 47,
        "candidate_id": candidate_id,
        "asset": asset,
        "timeframe": timeframe,
        "status": status,
        "strategy_family": family,
        "candidate": _candidate(candidate_id),
        "result": {
            "metrics": {
                "profit_factor": profit_factor,
                "expectancy_per_trade": expectancy,
                "number_of_trades": trades,
                "max_drawdown": drawdown,
            }
        },
    }


def test_parent_selection_prefers_promoted_30m_momentum_then_near_pass():
    rows = [
        _row(candidate_id="weak", asset="TSLA", timeframe="30m", status="rejected", family="Momentum", profit_factor=0.8, expectancy=-1, trades=80, drawdown=0.02),
        _row(candidate_id="near", asset="AMZN", timeframe="15m", status="rejected", family="Momentum", profit_factor=1.26, expectancy=5.3, trades=28, drawdown=0.04),
        _row(candidate_id="elite", asset="AMD", timeframe="30m", status="promoted", family="Momentum", profit_factor=1.55, expectancy=11.8, trades=48, drawdown=0.02),
    ]

    parents = select_low_timeframe_parent_rows(rows, parent_limit=3)

    assert [row["candidate_id"] for row in parents] == ["elite", "near"]


def test_variant_generation_preserves_parent_lineage_and_adds_source_evidence():
    row = _row(candidate_id="elite", asset="AMD", timeframe="30m", status="promoted", family="Momentum", profit_factor=1.55, expectancy=11.8, trades=48, drawdown=0.02)

    variants = generate_low_timeframe_variants(row, variants_per_parent=6)

    assert len(variants) == 6
    assert len({candidate.canonical_key for candidate in variants}) == 6
    assert all(candidate.parent_candidate_id == "elite" for candidate in variants)
    assert all(candidate.parameters["generation_channel"] == "low_timeframe_near_pass_expansion" for candidate in variants)
    assert variants[0].parameters["source_evidence"]["asset"] == "AMD"


def test_blueprint_defaults_to_30m_and_source_assets():
    rows = [
        _row(candidate_id="elite", asset="AMD", timeframe="30m", status="promoted", family="Momentum", profit_factor=1.55, expectancy=11.8, trades=48, drawdown=0.02),
        _row(candidate_id="near", asset="AMZN", timeframe="15m", status="rejected", family="Momentum", profit_factor=1.26, expectancy=5.3, trades=28, drawdown=0.04),
    ]

    blueprint = low_timeframe_expansion_blueprint(rows, parent_limit=2, variants_per_parent=3, asset_limit=5)

    assert blueprint["timeframes"] == ["30m"]
    assert blueprint["assets"] == ["AMD", "AMZN"]
    assert len(blueprint["parents"]) == 2
    assert len(blueprint["candidates"]) == 6
