"""Stage 0 probe: the measurements must be right before the numbers mean anything."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.intraday_execution_costs import aggregate_microstructure_bars
from app.services.microstructure_probe import (
    PRICE_CHANGE,
    SIZE_SAME_VENUE,
    VENUE_ROTATION,
    VENUE_ROTATION_KILL_THRESHOLD,
    QuoteStreamProbe,
    aggregate_probe_reports,
    cks_side_contributions,
    classify_side_event,
    parse_rfc3339_nanoseconds,
    rotation_verdict,
    stage0_power_report,
)


def quote(timestamp: str, bp: float, bs: float, ap: float, asz: float, bx="P", ax="P"):
    return {"t": timestamp, "bp": bp, "bs": bs, "ap": ap, "as": asz, "bx": bx, "ax": ax}


# ---------------------------------------------------------------------------
# Nanosecond parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected_tail",
    [
        ("2026-06-01T13:30:00Z", 0),
        ("2026-06-01T13:30:00.1Z", 100_000_000),
        ("2026-06-01T13:30:00.000000001Z", 1),
        ("2026-06-01T13:30:00.123456789Z", 123_456_789),
    ],
)
def test_nanosecond_parsing_keeps_full_precision(value, expected_tail):
    parsed = parse_rfc3339_nanoseconds(value)
    assert parsed % 1_000_000_000 == expected_tail


def test_nanosecond_parsing_distinguishes_sub_microsecond_updates():
    """The exact case the current schema collapses."""
    first = parse_rfc3339_nanoseconds("2026-06-01T13:30:00.123456111Z")
    second = parse_rfc3339_nanoseconds("2026-06-01T13:30:00.123456999Z")
    assert first != second
    assert first // 1_000 == second // 1_000  # identical at microsecond precision


# ---------------------------------------------------------------------------
# CKS kernel: must match the implementation already in production
# ---------------------------------------------------------------------------


def test_side_contributions_sum_to_the_existing_production_kernel():
    rows = [
        quote("2026-06-01T13:30:00Z", 10.00, 100, 10.02, 200),
        quote("2026-06-01T13:30:01Z", 10.01, 300, 10.02, 150),
        quote("2026-06-01T13:30:02Z", 10.01, 250, 10.03, 400),
        quote("2026-06-01T13:30:03Z", 10.00, 120, 10.03, 380),
    ]
    normalized = [
        {
            "symbol": "T",
            "provider": "alpaca",
            "feed": "sip",
            "timestamp": __import__("datetime").datetime.fromisoformat(
                row["t"].replace("Z", "+00:00")
            ),
            "bid_price": row["bp"],
            "ask_price": row["ap"],
            "bid_size": row["bs"],
            "ask_size": row["as"],
            "spread_bps": (row["ap"] - row["bp"]) / ((row["ap"] + row["bp"]) / 2) * 10_000,
        }
        for row in rows
    ]
    production = aggregate_microstructure_bars(normalized, timeframe="15m")
    assert len(production) == 1

    probe_total = 0.0
    for previous, current in zip(normalized, normalized[1:]):
        bid, ask = cks_side_contributions(previous, current)
        probe_total += bid + ask

    assert probe_total == pytest.approx(float(production[0]["order_flow_imbalance"]))


# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------


def test_classification_separates_the_three_reasons():
    assert (
        classify_side_event(
            previous_price=10.0, current_price=10.01, previous_venue="P", current_venue="P"
        )
        == PRICE_CHANGE
    )
    assert (
        classify_side_event(
            previous_price=10.0, current_price=10.0, previous_venue="P", current_venue="Q"
        )
        == VENUE_ROTATION
    )
    assert (
        classify_side_event(
            previous_price=10.0, current_price=10.0, previous_venue="P", current_venue="P"
        )
        == SIZE_SAME_VENUE
    )


def test_price_change_wins_over_a_simultaneous_venue_change():
    """A price move is a real book event even if the venue also rotated."""
    assert (
        classify_side_event(
            previous_price=10.0, current_price=10.01, previous_venue="P", current_venue="Q"
        )
        == PRICE_CHANGE
    )


# ---------------------------------------------------------------------------
# The headline measurement
# ---------------------------------------------------------------------------


def test_pure_venue_rotation_is_attributed_entirely_to_rotation():
    """Two venues tied at the same NBBO price, alternating. No real liquidity moved."""
    probe = QuoteStreamProbe(symbol="T", session_date=date(2026, 6, 1), feed="sip")
    rows = [
        quote("2026-06-01T13:30:00.000000001Z", 10.00, 100, 10.01, 100, bx="P", ax="P"),
        quote("2026-06-01T13:30:00.000000002Z", 10.00, 900, 10.01, 100, bx="Q", ax="P"),
        quote("2026-06-01T13:30:00.000000003Z", 10.00, 100, 10.01, 100, bx="P", ax="P"),
    ]
    probe.add_page(rows, {"exhausted": True})
    report = probe.report()
    assert report["venue_rotation"]["rotation_share_of_gross_abs_e"] == pytest.approx(1.0)
    assert report["venue_rotation"]["update_rotation_rate"] == pytest.approx(1.0)


def test_genuine_same_venue_activity_is_not_charged_to_rotation():
    probe = QuoteStreamProbe(symbol="T", session_date=date(2026, 6, 1), feed="sip")
    rows = [
        quote("2026-06-01T13:30:00Z", 10.00, 100, 10.01, 100, bx="P", ax="P"),
        quote("2026-06-01T13:30:01Z", 10.00, 400, 10.01, 100, bx="P", ax="P"),
        quote("2026-06-01T13:30:02Z", 10.01, 200, 10.02, 100, bx="P", ax="P"),
    ]
    probe.add_page(rows, {"exhausted": True})
    report = probe.report()
    assert report["venue_rotation"]["rotation_share_of_gross_abs_e"] == pytest.approx(0.0)


def test_rotation_share_is_a_ratio_of_magnitudes_not_of_counts():
    """One large rotation must outweigh many tiny same-venue ticks."""
    probe = QuoteStreamProbe(symbol="T", session_date=date(2026, 6, 1), feed="sip")
    rows = [quote("2026-06-01T13:30:00Z", 10.00, 100, 10.01, 100, bx="P", ax="P")]
    for index in range(9):
        rows.append(
            quote(
                f"2026-06-01T13:30:{index + 1:02d}Z",
                10.00,
                101 + index,
                10.01,
                100,
                bx="P",
                ax="P",
            )
        )
    rows.append(quote("2026-06-01T13:30:20Z", 10.00, 10_000, 10.01, 100, bx="Q", ax="P"))
    probe.add_page(rows, {"exhausted": True})
    share = probe.report()["venue_rotation"]["rotation_share_of_gross_abs_e"]
    assert share > 0.9


# ---------------------------------------------------------------------------
# Collapse accounting
# ---------------------------------------------------------------------------


def test_microsecond_collapse_counts_rows_the_current_schema_would_lose():
    probe = QuoteStreamProbe(symbol="T", session_date=date(2026, 6, 1), feed="sip")
    rows = [
        quote("2026-06-01T13:30:00.123456111Z", 10.00, 100, 10.01, 100),
        quote("2026-06-01T13:30:00.123456222Z", 10.00, 200, 10.01, 100),
        quote("2026-06-01T13:30:00.123456333Z", 10.00, 300, 10.01, 100),
        quote("2026-06-01T13:30:00.123457000Z", 10.00, 400, 10.01, 100),
    ]
    probe.add_page(rows, {"exhausted": True})
    fidelity = probe.report()["timestamp_fidelity"]
    assert fidelity["distinct_nanosecond_instants"] == 4
    assert fidelity["distinct_microsecond_instants"] == 2
    assert fidelity["rows_lost_to_microsecond_truncation"] == 2
    assert fidelity["microsecond_collapse_rate"] == pytest.approx(0.5)


def test_halt_proxy_records_long_quote_gaps():
    probe = QuoteStreamProbe(symbol="T", session_date=date(2026, 6, 1), feed="sip")
    rows = [
        quote("2026-06-01T13:30:00Z", 10.00, 100, 10.01, 100),
        quote("2026-06-01T14:30:00Z", 10.00, 100, 10.01, 100),
    ]
    probe.add_page(rows, {"exhausted": True})
    health = probe.report()["stream_health"]
    assert health["quote_gaps_over_halt_proxy"] == 1
    assert health["max_quote_gap_seconds"] == pytest.approx(3600.0)


def test_crossed_and_locked_quotes_are_counted_not_silently_dropped():
    probe = QuoteStreamProbe(symbol="T", session_date=date(2026, 6, 1), feed="sip")
    rows = [
        quote("2026-06-01T13:30:00Z", 10.02, 100, 10.01, 100),  # crossed
        quote("2026-06-01T13:30:01Z", 10.01, 100, 10.01, 100),  # locked
    ]
    probe.add_page(rows, {"exhausted": True})
    health = probe.report()["stream_health"]
    assert health["crossed_quotes"] == 1
    assert health["locked_quotes"] == 1
    assert health["silent_normalizer_rejections"]["crossed"] == 1


# ---------------------------------------------------------------------------
# Kill rule and pooling
# ---------------------------------------------------------------------------


def test_kill_rule_uses_the_predeclared_threshold_and_nothing_else():
    assert VENUE_ROTATION_KILL_THRESHOLD == 0.30
    assert rotation_verdict(0.30)["verdict"] == "within_threshold"
    assert rotation_verdict(0.3001)["verdict"] == "exceeds_threshold"
    assert rotation_verdict(None)["verdict"] == "not_measurable"


def test_pooling_weights_by_magnitude_across_symbol_sessions():
    reports = [
        {
            "quotes_parsed": 100,
            "quotes_per_second": 1.0,
            "window_exhausted": True,
            "timestamp_fidelity": {"rows_lost_to_microsecond_truncation": 10},
            "venue_rotation": {
                "gross_abs_e": 100.0,
                "gross_abs_e_by_reason": {VENUE_ROTATION: 10.0},
                "updates_compared": 50,
                "updates_with_any_rotation": 5,
            },
            "stream_health": {"median_spread_bps": 2.0},
        },
        {
            "quotes_parsed": 100,
            "quotes_per_second": 3.0,
            "window_exhausted": True,
            "timestamp_fidelity": {"rows_lost_to_microsecond_truncation": 30},
            "venue_rotation": {
                "gross_abs_e": 900.0,
                "gross_abs_e_by_reason": {VENUE_ROTATION: 450.0},
                "updates_compared": 50,
                "updates_with_any_rotation": 25,
            },
            "stream_health": {"median_spread_bps": 4.0},
        },
    ]
    pooled = aggregate_probe_reports(reports)
    assert pooled["rotation_share_of_gross_abs_e"] == pytest.approx(460.0 / 1000.0)
    assert pooled["microsecond_collapse_rate"] == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------


def test_power_report_separates_detectability_from_materiality():
    report = stage0_power_report(
        minimum_tradeable_net_bps=5.0,
        declared_dispersion_bps=60.0,
        hurdle_t=3.0,
        power_z=0.841621,
        round_trip_cost_bps=3.0,
        cost_safety_multiple=2.0,
    )
    incremental = report["materiality"]["regime_incremental"]
    # The literature effect is far below the smallest effect declared tradeable.
    assert incremental["clears_minimum_tradeable"] is False
    assert incremental["clears_cost_hurdle"] is False
    assert incremental["shortfall_multiple_vs_minimum"] > 10
    # But it is not hard to *detect*: the two questions must not be conflated.
    assert report["detectability"]["regime_incremental"]["required_independent_events"] < 5_000


def test_power_effect_matches_the_published_table():
    report = stage0_power_report(
        minimum_tradeable_net_bps=5.0,
        declared_dispersion_bps=60.0,
        hurdle_t=3.0,
        power_z=0.841621,
        round_trip_cost_bps=3.0,
        cost_safety_multiple=2.0,
    )
    moves = report["conditional_expected_move_bps"]
    assert moves["ask_heavy"] == pytest.approx(0.0)
    assert moves["neutral"] == pytest.approx(0.593, abs=0.01)
    assert moves["bid_heavy"] == pytest.approx(0.949, abs=0.01)
