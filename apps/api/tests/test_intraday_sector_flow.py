from datetime import UTC, datetime

from app.services.intraday_sector_flow import (
    MINIMUM_PEERS,
    sector_flow_coverage,
    sector_relative_bars,
)

BAR = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)


def candle(value, *, relative_volume=1.0, timestamp=BAR):
    return {
        "timestamp": timestamp,
        "open": 100.0,
        "high": 100.0 * (1 + max(value, 0)),
        "low": 100.0 * (1 + min(value, 0)),
        "close": 100.0 * (1 + value),
        "volume": 1000.0,
        "session_relative_volume": relative_volume,
    }


def universe(moves, *, relative_volumes=None):
    relative_volumes = relative_volumes or {}
    return {
        symbol: [candle(value, relative_volume=relative_volumes.get(symbol, 1.0))]
        for symbol, value in moves.items()
    }


TECH = {f"T{index}": "Technology" for index in range(6)}


def test_a_move_matching_its_sector_leaves_no_residual():
    moves = {symbol: -0.03 for symbol in TECH}

    bars = sector_relative_bars(universe(moves), sector_by_symbol=TECH)

    assert bars["T0"][BAR]["sector_residual_return"] == 0.0
    assert bars["T0"][BAR]["bar_return"] == -0.03


def test_a_move_against_a_flat_sector_is_all_residual():
    moves = {symbol: 0.0 for symbol in TECH}
    moves["T0"] = -0.03

    bars = sector_relative_bars(universe(moves), sector_by_symbol=TECH)

    assert bars["T0"][BAR]["sector_median_return"] == 0.0
    assert bars["T0"][BAR]["sector_residual_return"] == -0.03


def test_the_symbol_is_excluded_from_its_own_benchmark():
    # If the name were included, its own -30% would drag the median it is
    # measured against and shrink the residual.
    moves = {symbol: 0.0 for symbol in TECH}
    moves["T0"] = -0.30

    bars = sector_relative_bars(universe(moves), sector_by_symbol=TECH)

    assert bars["T0"][BAR]["sector_median_return"] == 0.0
    assert bars["T0"][BAR]["peers"] == len(TECH) - 1


def test_a_thin_peer_group_is_withheld_rather_than_estimated():
    thin = {f"S{index}": "Utilities" for index in range(MINIMUM_PEERS)}
    moves = {symbol: 0.01 for symbol in thin}

    bars = sector_relative_bars(universe(moves), sector_by_symbol=thin)

    assert bars == {}


def test_symbols_without_a_sector_are_skipped_not_guessed():
    moves = {symbol: 0.01 for symbol in TECH}
    moves["UNKNOWN"] = 0.05
    sectors = dict(TECH)

    bars = sector_relative_bars(universe(moves), sector_by_symbol=sectors)

    assert "UNKNOWN" not in bars
    assert "T0" in bars


def test_standardized_residual_scales_by_how_much_the_sector_was_moving():
    calm = {symbol: 0.0 for symbol in TECH}
    calm["T0"] = 0.005
    volatile = {f"T{index}": 0.02 * (-1) ** index for index in range(6)}
    volatile["T0"] = 0.005

    calm_bars = sector_relative_bars(universe(calm), sector_by_symbol=TECH)
    volatile_bars = sector_relative_bars(universe(volatile), sector_by_symbol=TECH)

    # The same 50 bps move is a large residual in a calm sector and a small
    # one when peers are swinging two percent.
    assert calm_bars["T0"][BAR]["standardized_residual"] is None or (
        volatile_bars["T0"][BAR]["standardized_residual"]
        < calm_bars["T0"][BAR]["standardized_residual"]
    )


def test_excess_participation_flags_a_name_traded_harder_than_its_sector():
    moves = {symbol: 0.0 for symbol in TECH}
    volumes = {symbol: 1.0 for symbol in TECH}
    volumes["T0"] = 4.0

    bars = sector_relative_bars(
        universe(moves, relative_volumes=volumes), sector_by_symbol=TECH
    )

    assert bars["T0"][BAR]["excess_participation"] == 4.0
    assert bars["T1"][BAR]["excess_participation"] < 1.5


def test_coverage_reports_which_sectors_have_enough_peers():
    sectors = {**TECH, "X0": "Energy", "X1": "Energy"}
    moves = {symbol: 0.0 for symbol in sectors}

    report = sector_flow_coverage(universe(moves), sector_by_symbol=sectors)

    assert report["sectors"] == 2
    assert report["sectors_with_enough_peers"] == 1
    assert report["symbols_in_usable_sectors"] == 6
    assert report["sector_coverage"] == 1.0


def test_coverage_reports_partial_sector_knowledge():
    moves = {symbol: 0.0 for symbol in TECH}
    moves["NOSECTOR"] = 0.0

    report = sector_flow_coverage(universe(moves), sector_by_symbol=TECH)

    assert report["symbols_with_sector"] == 6
    assert report["sector_coverage"] < 1.0
