"""Stage 4.0 audit tests.

The verdict logic is pure, so it is tested against constructed facts rather
than against whatever the production database happens to contain today. That
matters here more than usual: the whole point of the audit is to be right about
what we *don't* have, and a test that can only run where the data exists cannot
check the absence cases.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta

import pytest

from app.services.stage40_audit import (
    AUDITED_TABLES,
    EventSupply,
    OptionQuality,
    TableCoverage,
    cross_source_overlap,
    event_supply_adequacy,
    mbo_state_feature_feasibility,
    options_feasibility,
    put_call_parity_feasible,
    recommend,
    signed_flow_requirements,
    stock_flow_feasibility,
    summarise_event_supply,
)
from app.services.stage40_audit_plan import (
    AUDIT_WINDOWS,
    CERTIFIED_L3_WINDOW,
    EFFECTIVE_TRIALS_AFTER,
    EFFECTIVE_TRIALS_BEFORE,
    MIN_EVENTS_FOR_MECHANISM,
    MIN_SESSIONS_FOR_MECHANISM,
    MISNAMED_OPTION_FEATURES,
    OPTIONS_COLLECTION_WINDOW,
    OPTIONS_NOT_SUITABLE,
    OPTIONS_SIGNED_FLOW,
    OPTIONS_STATE_ONLY,
    RECOMMEND_IAG,
    RECOMMEND_NO_SUPPLY,
    RECOMMENDATIONS,
    TIMESTAMP_REGISTRY,
    alignment_resolution_ns,
    assert_decision_safe,
    statistical_plan,
    timestamp_semantics,
)

SECOND_NS = 1_000_000_000
MICROSECOND_NS = 1_000


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def test_the_ledger_does_not_move():
    """No economic specification is tested, so no trial is spent."""
    assert EFFECTIVE_TRIALS_BEFORE == 531
    assert EFFECTIVE_TRIALS_AFTER == 531
    plan = statistical_plan()
    assert plan["effective_trials_before"] == plan["effective_trials_after"] == 531


def test_the_plan_declares_itself_outcome_blind():
    plan = statistical_plan()
    assert plan["contains_strategy_outcome"] is False
    assert plan["contains_post_decision_return"] is False
    assert plan["contains_pnl"] is False


def test_the_plan_declares_its_thresholds_before_any_measurement():
    """A floor chosen after seeing the count it judges is not a floor."""
    plan = statistical_plan()
    assert plan["min_events_for_mechanism"] == MIN_EVENTS_FOR_MECHANISM == 100
    assert plan["min_sessions_for_mechanism"] == MIN_SESSIONS_FOR_MECHANISM == 15


def test_both_windows_are_declared_separately():
    """The two richest sources live in different years and are not merged."""
    names = [w.name for w in AUDIT_WINDOWS]
    assert names == ["certified_l3_2025_06", "options_2026_collection_window"]
    assert CERTIFIED_L3_WINDOW.start_date == "2025-06-02"
    assert CERTIFIED_L3_WINDOW.end_date == "2025-06-30"
    assert len(CERTIFIED_L3_WINDOW.symbols) == 8
    # Open-ended on purpose: the collector still runs, so a hard end date here
    # would silently truncate the measurement.
    assert OPTIONS_COLLECTION_WINDOW.end_date is None
    assert OPTIONS_COLLECTION_WINDOW.is_open_ended


# ---------------------------------------------------------------------------
# Timestamp semantics
# ---------------------------------------------------------------------------


def test_the_news_event_clock_is_known_at_and_is_whole_second():
    entry = timestamp_semantics("intraday_news_articles", "known_at")
    assert entry.kind == "event"
    assert entry.decision_safe is True
    assert entry.resolution_ns == SECOND_NS


def test_the_news_receive_clock_is_refused():
    """received_at is DEFAULT NOW(): a 2026 clock on a 2025 event."""
    entry = timestamp_semantics("intraday_news_articles", "received_at")
    assert entry.kind == "backfill"
    assert entry.decision_safe is False
    with pytest.raises(ValueError, match="not safe at decision time"):
        assert_decision_safe("intraday_news_articles", "received_at")


def test_an_undeclared_clock_fails_closed():
    """The dangerous timestamp is the one nobody reasoned about."""
    with pytest.raises(ValueError, match="no declared timestamp semantics"):
        timestamp_semantics("intraday_news_articles", "some_new_column")
    with pytest.raises(ValueError, match="no declared timestamp semantics"):
        timestamp_semantics("a_table_nobody_declared", "timestamp")


def test_the_derived_nanosecond_column_is_not_treated_as_nanoseconds():
    """timestamp_ns is named nanoseconds but migration 080 derived it by
    scaling microseconds. Claiming nanosecond resolution would fabricate
    precision the rows never had."""
    entry = timestamp_semantics("intraday_quote_snapshots", "timestamp_ns")
    assert entry.kind == "derived"
    assert entry.resolution_ns == MICROSECOND_NS
    assert entry.resolution_ns != 1


def test_the_mbo_clock_is_genuinely_nanosecond():
    entry = timestamp_semantics("mbo_features_parquet", "feature_available_ts_recv")
    assert entry.resolution_ns == 1
    assert entry.decision_safe is True


def test_operational_clocks_are_refused():
    with pytest.raises(ValueError, match="not safe at decision time"):
        assert_decision_safe("intraday_option_chain_snapshots", "created_at")


def test_alignment_takes_the_coarsest_clock_not_the_finest():
    """Joining a nanosecond book to a whole-second news stamp gives one second."""
    resolution = alignment_resolution_ns(
        [
            ("mbo_features_parquet", "feature_available_ts_recv"),
            ("intraday_news_articles", "known_at"),
        ]
    )
    assert resolution == SECOND_NS

    options_only = alignment_resolution_ns(
        [("intraday_option_chain_snapshots", "observed_at")]
    )
    assert options_only == MICROSECOND_NS


def test_alignment_refuses_an_unsafe_clock():
    with pytest.raises(ValueError, match="not safe at decision time"):
        alignment_resolution_ns(
            [
                ("intraday_news_articles", "known_at"),
                ("intraday_news_articles", "received_at"),
            ]
        )


def test_alignment_refuses_an_empty_join():
    with pytest.raises(ValueError, match="alignment resolution is undefined"):
        alignment_resolution_ns([])


def test_every_audited_table_has_a_declared_clock():
    """A table with no declared clock cannot have coverage measured."""
    for table, clock, _symbol in AUDITED_TABLES:
        entry = timestamp_semantics(table, clock)
        assert entry.decision_safe, f"{table}.{clock} is audited but not safe"


def test_the_registry_declares_a_resolution_and_note_for_every_clock():
    for entry in TIMESTAMP_REGISTRY:
        assert entry.resolution_ns > 0, entry.column
        assert entry.timezone == "UTC", entry.column
        assert entry.note.strip(), entry.column


# ---------------------------------------------------------------------------
# Options field interpretation
# ---------------------------------------------------------------------------


def test_signed_option_flow_is_blocked_by_the_missing_sequence():
    """A latest-trade snapshot cannot be classified, whatever else is present."""
    requirements = signed_flow_requirements()
    assert requirements["trade_sequence"] is False
    assert requirements["trade_conditions"] is False
    assert requirements["exchange"] is False
    assert requirements["cumulative_volume"] is False
    # Quotes ARE present -- the block is the sequence, not the quote.
    assert requirements["quote_at_trade_time"] is True


def test_options_verdict_is_state_only_when_rows_exist():
    coverage = TableCoverage(
        table="intraday_option_chain_snapshots",
        window="options_2026_collection_window",
        rows=5_000_000,
        first_instant="2026-08-14T13:30:00+00:00",
        last_instant="2026-08-20T20:00:00+00:00",
        distinct_symbols=48,
        distinct_days=5,
    )
    result = options_feasibility(coverage=coverage)
    assert result["verdict"] == OPTIONS_STATE_ONLY
    assert result["verdict"] != OPTIONS_SIGNED_FLOW
    assert "trade_sequence" in result["blocking_for_signed_flow"]
    assert result["state_variables_available"]


def test_options_verdict_is_not_suitable_when_the_window_is_empty():
    """Availability is checked before field quality. A perfect schema over a
    window holding no rows is not 'state only'; it is not usable."""
    empty = TableCoverage(
        table="intraday_option_chain_snapshots",
        window="certified_l3_2025_06",
        rows=0,
        first_instant=None,
        last_instant=None,
        distinct_symbols=0,
        distinct_days=0,
    )
    result = options_feasibility(coverage=empty)
    assert result["verdict"] == OPTIONS_NOT_SUITABLE
    assert "no option-chain rows" in result["reason"]
    assert result["state_variables_available"] == []

    assert options_feasibility(coverage=None)["verdict"] == OPTIONS_NOT_SUITABLE


def test_the_misnamed_volume_features_are_named_in_every_verdict():
    """They read as flow to anyone who does not check the source."""
    for coverage in (None, _option_coverage(rows=10)):
        result = options_feasibility(coverage=coverage)
        assert result["misnamed_fields_not_to_use"] == list(MISNAMED_OPTION_FEATURES)
    assert "option_call_volume" in MISNAMED_OPTION_FEATURES
    assert "option_put_volume" in MISNAMED_OPTION_FEATURES


def test_no_option_field_is_marked_usable_as_flow():
    from app.services.stage40_audit_plan import OPTION_FIELD_SEMANTICS

    assert not [f.column for f in OPTION_FIELD_SEMANTICS if f.usable_as_flow]


def test_absent_option_fields_are_declared_not_merely_omitted():
    """An inventory listing only what is present cannot answer 'can we do X'."""
    from app.services.stage40_audit_plan import OPTION_FIELD_SEMANTICS

    absent = {f.column for f in OPTION_FIELD_SEMANTICS if not f.present}
    for expected in (
        "volume",
        "underlying_price",
        "exchange",
        "trade_conditions",
        "trade_sequence",
        "is_nbbo",
    ):
        assert expected in absent, expected


def test_put_call_parity_is_blocked_by_the_missing_underlying_price():
    result = put_call_parity_feasible()
    assert result["feasible_from_option_table_alone"] is False
    assert result["missing"] == ["underlying_price"]
    assert result["atm_anchor_fallback"] == "median listed strike"


def test_option_quality_shares_are_none_on_an_empty_window():
    """Dividing by zero rows must not fabricate a 0% or 100% quality claim."""
    quality = OptionQuality(
        window="w",
        rows=0,
        rows_with_quote=0,
        rows_with_trade=0,
        rows_with_iv=0,
        rows_with_greeks=0,
        rows_with_open_interest=0,
        crossed_or_locked=0,
        max_quote_staleness_seconds=None,
        median_quote_staleness_seconds=None,
    )
    payload = quality.as_dict()
    assert payload["quote_present_share"] is None
    assert payload["crossed_or_locked_share"] is None


def test_option_quality_shares_are_computed_when_rows_exist():
    quality = OptionQuality(
        window="w",
        rows=1000,
        rows_with_quote=950,
        rows_with_trade=400,
        rows_with_iv=900,
        rows_with_greeks=900,
        rows_with_open_interest=1000,
        crossed_or_locked=3,
        max_quote_staleness_seconds=612.5,
        median_quote_staleness_seconds=1.25,
    )
    payload = quality.as_dict()
    assert payload["quote_present_share"] == 0.95
    assert payload["crossed_or_locked_share"] == 0.003
    assert payload["max_quote_staleness_seconds"] == 612.5


# ---------------------------------------------------------------------------
# Stock flow
# ---------------------------------------------------------------------------


def test_market_wide_signed_flow_is_not_sufficient():
    result = stock_flow_feasibility(
        trade_flow_coverage=_coverage("intraday_trade_flow_features", rows=100_000),
        quote_coverage=_coverage("intraday_quote_snapshots", rows=23_016_760),
        decertified_fields=["intraday_quote_snapshots.order_flow_imbalance"],
    )
    assert result["sufficient_for_market_wide_signed_flow"] is False
    assert result["raw_trade_prints_persisted"] is False
    assert result["quoted_size_usable_as_depth"] is False
    assert result["finest_signed_flow_resolution_ns"] == 15 * 60 * SECOND_NS


def test_the_missing_data_list_names_the_specific_sources():
    result = stock_flow_feasibility(trade_flow_coverage=None, quote_coverage=None)
    needed = {item["data"] for item in result["would_require"]}
    assert "SIP / TAQ trade prints" in needed
    assert "exchange identifiers" in needed
    assert "trade condition codes" in needed


def test_the_l3_features_are_not_retired_by_name_collision():
    """Stage 0 decertified quoted-size fields on intraday_quote_snapshots. The
    identically named MBO features are book-derived and unaffected -- retiring
    them by name would discard good data."""
    result = stock_flow_feasibility(
        trade_flow_coverage=None,
        quote_coverage=None,
        decertified_fields=["intraday_quote_snapshots.order_flow_imbalance"],
    )
    assert "NOT covered by that" in result["decertification_note"]

    from app.cli.mbo_stage2 import FEATURE_NAMES

    assert "order_flow_imbalance" in FEATURE_NAMES
    state = mbo_state_feature_feasibility(FEATURE_NAMES)
    assert state["concepts"]["persistent_aggressive_direction"]["constructible"]


# ---------------------------------------------------------------------------
# L3 state feasibility
# ---------------------------------------------------------------------------


def test_the_certified_vocabulary_supports_the_requested_states():
    from app.cli.mbo_stage2 import FEATURE_NAMES

    state = mbo_state_feature_feasibility(FEATURE_NAMES)
    assert state["vocabulary_size"] == 59
    for concept in (
        "depth_consumed",
        "replenishment_after_consumption",
        "cancellation_addition_imbalance",
        "persistent_aggressive_direction",
        "spread_depth_stress",
        "liquidity_vacuum_state",
        "absorption",
    ):
        assert concept in state["constructible_now"], concept


def test_local_lambda_is_reported_as_the_one_gap():
    from app.cli.mbo_stage2 import FEATURE_NAMES

    state = mbo_state_feature_feasibility(FEATURE_NAMES)
    assert "local_lambda_price_sensitivity" in state["gaps"]
    assert state["gap_count"] == 1
    # The gap note must say why building it naively is a forward return.
    assert "forward return" in state["gaps"]["local_lambda_price_sensitivity"]


def test_a_missing_feature_downgrades_its_concept_rather_than_rounding_up():
    """Partial coverage is reported as partial."""
    from app.cli.mbo_stage2 import FEATURE_NAMES

    reduced = [f for f in FEATURE_NAMES if f != "absorption_ratio"]
    state = mbo_state_feature_feasibility(reduced)
    assert not state["concepts"]["absorption"]["constructible"]
    assert state["concepts"]["absorption"]["missing_features"] == ["absorption_ratio"]
    assert "absorption" in state["partially_backed"]
    assert "absorption" not in state["constructible_now"]


# ---------------------------------------------------------------------------
# Event supply -- deterministic counts
# ---------------------------------------------------------------------------


def _story(symbol: str, moment: datetime) -> dict[str, object]:
    return {"symbol": symbol, "story_id": f"{symbol}-{moment.isoformat()}", "known_at": moment}


BASE = datetime(2025, 6, 2, 14, 0, tzinfo=UTC)


def test_the_quiet_period_collapses_a_cluster_to_one_event():
    """Five stories inside one quiet window are one isolated event, not five."""
    rows = [_story("AAPL", BASE + timedelta(minutes=5 * i)) for i in range(5)]
    supply = summarise_event_supply(rows, window="w", quiet_minutes=60)
    assert supply.raw_events == 5
    assert supply.isolated_events == 1


def test_a_story_exactly_at_the_quiet_boundary_is_isolated():
    """Inclusive at the boundary, matching Stage 3.6's declared rule."""
    rows = [_story("AAPL", BASE), _story("AAPL", BASE + timedelta(minutes=60))]
    supply = summarise_event_supply(rows, window="w", quiet_minutes=60)
    assert supply.isolated_events == 2

    one_ns_short = [_story("AAPL", BASE), _story("AAPL", BASE + timedelta(minutes=59, seconds=59))]
    assert summarise_event_supply(one_ns_short, window="w", quiet_minutes=60).isolated_events == 1


def test_the_quiet_period_counts_non_isolated_stories_too():
    """A suppressed story still resets the clock. Otherwise a steady drip of
    50-minute-spaced stories would all count as isolated."""
    rows = [_story("AAPL", BASE + timedelta(minutes=50 * i)) for i in range(4)]
    supply = summarise_event_supply(rows, window="w", quiet_minutes=60)
    assert supply.isolated_events == 1


def test_symbols_are_isolated_independently():
    rows = [_story("AAPL", BASE), _story("MSFT", BASE), _story("NVDA", BASE)]
    supply = summarise_event_supply(rows, window="w", quiet_minutes=60)
    assert supply.isolated_events == 3
    assert supply.raw_distinct_symbols == 3


def test_event_counts_are_deterministic():
    """Same input, same counts, every time and in any repetition."""
    rows = [
        _story("AAPL", BASE + timedelta(minutes=90 * i)) for i in range(4)
    ] + [_story("MSFT", BASE + timedelta(minutes=70 * i)) for i in range(3)]
    first = summarise_event_supply(rows, window="w", quiet_minutes=60)
    second = summarise_event_supply(rows, window="w", quiet_minutes=60)
    assert first.as_dict() == second.as_dict()
    assert first.isolated_events == 7


def test_coverage_gating_uses_session_membership_not_price():
    """Eligibility is decided from timestamps and coverage alone."""
    rows = [
        _story("AAPL", BASE),
        _story("AAPL", BASE + timedelta(days=1, minutes=1)),
    ]
    supply = summarise_event_supply(
        rows,
        window="w",
        quiet_minutes=60,
        l3_coverage=_coverage_for([("AAPL", "2025-06-02")]),
        option_days=[],
    )
    assert supply.isolated_events == 2
    assert supply.with_l3_coverage == 1
    assert supply.with_option_observation == 0
    assert supply.with_all_sources == 0


def test_all_three_sources_requires_both_coverages():
    rows = [_story("AAPL", BASE)]
    both = summarise_event_supply(
        rows,
        window="w",
        quiet_minutes=60,
        l3_coverage=_coverage_for([("AAPL", "2025-06-02")]),
        option_days=["2025-06-02"],
    )
    assert both.with_all_sources == 1
    assert both.all_source_distinct_sessions == 1

    l3_only = summarise_event_supply(
        rows,
        window="w",
        quiet_minutes=60,
        l3_coverage=_coverage_for([("AAPL", "2025-06-02")]),
        option_days=[],
    )
    assert l3_only.with_all_sources == 0
    assert l3_only.all_source_distinct_sessions == 0


def test_a_naive_timestamp_fails_closed():
    """A timestamp without a timezone is not an instant."""
    # Deliberately naive: this is the input the audit must refuse.
    naive = datetime(2025, 6, 2, 14, 0)  # noqa: DTZ001
    rows = [{"symbol": "AAPL", "story_id": "x", "known_at": naive}]
    with pytest.raises(ValueError, match="carries no timezone"):
        summarise_event_supply(rows, window="w", quiet_minutes=60)


def test_hour_histogram_is_utc_and_sorted():
    rows = [
        _story("AAPL", BASE),
        _story("MSFT", BASE.replace(hour=19)),
        _story("NVDA", BASE.replace(hour=19)),
    ]
    supply = summarise_event_supply(rows, window="w", quiet_minutes=60)
    assert supply.as_dict()["by_hour_utc"] == {"14": 1, "19": 2}
    assert supply.as_dict()["raw_distinct_days"] == 1


def test_an_empty_population_counts_zero_rather_than_failing():
    supply = summarise_event_supply([], window="w", quiet_minutes=60)
    assert supply.raw_events == 0
    assert supply.isolated_events == 0
    assert supply.raw_distinct_days == 0
    assert supply.l3_covered_distinct_sessions == 0


# ---------------------------------------------------------------------------
# Adequacy and recommendation
# ---------------------------------------------------------------------------


def _supply(events: int, sessions: int, window: str = "w") -> EventSupply:
    """A supply record whose L3-covered session count is what the gate reads.

    ``raw_distinct_days`` is deliberately set higher than the covered session
    count: news arrives on weekends and overnight, and the gate must not see
    those days.
    """
    return EventSupply(
        window=window,
        raw_events=events * 2,
        isolated_events=events,
        with_l3_coverage=events,
        with_option_observation=0,
        with_all_sources=0,
        raw_distinct_symbols=8,
        raw_distinct_days=sessions + 9,
        isolated_distinct_days=sessions + 4,
        l3_covered_distinct_sessions=sessions,
        option_covered_distinct_sessions=0,
        all_source_distinct_sessions=0,
        by_hour_utc={},
    )


def test_adequacy_requires_both_floors():
    assert event_supply_adequacy(_supply(100, 15))["adequate"] is True
    assert event_supply_adequacy(_supply(99, 15))["adequate"] is False
    assert event_supply_adequacy(_supply(100, 14))["adequate"] is False


def test_insufficient_supply_outranks_a_rich_feature_set():
    """A mechanism nobody can evaluate is not a mechanism."""
    from app.cli.mbo_stage2 import FEATURE_NAMES

    result = recommend(
        l3_state=mbo_state_feature_feasibility(FEATURE_NAMES),
        options=options_feasibility(coverage=None),
        stock_flow=stock_flow_feasibility(trade_flow_coverage=None, quote_coverage=None),
        supply=[event_supply_adequacy(_supply(12, 3))],
        overlap={},
    )
    assert result["recommendation"] == RECOMMEND_NO_SUPPLY


def test_adequate_supply_with_l3_state_recommends_the_iag_design():
    from app.cli.mbo_stage2 import FEATURE_NAMES

    result = recommend(
        l3_state=mbo_state_feature_feasibility(FEATURE_NAMES),
        options=options_feasibility(coverage=None),
        stock_flow=stock_flow_feasibility(trade_flow_coverage=None, quote_coverage=None),
        supply=[event_supply_adequacy(_supply(168, 20))],
        overlap={},
    )
    assert result["recommendation"] == RECOMMEND_IAG
    assert result["recommendation"] in RECOMMENDATIONS


def test_the_ranking_puts_blocked_mechanisms_last():
    from app.cli.mbo_stage2 import FEATURE_NAMES

    result = recommend(
        l3_state=mbo_state_feature_feasibility(FEATURE_NAMES),
        options=options_feasibility(coverage=_option_coverage(rows=1000)),
        stock_flow=stock_flow_feasibility(trade_flow_coverage=None, quote_coverage=None),
        supply=[event_supply_adequacy(_supply(168, 20))],
        overlap={},
    )
    ranked = result["ranked_mechanisms"]
    assert [m["rank"] for m in ranked] == list(range(1, len(ranked) + 1))
    assert ranked[-1]["mechanism"] == "market_wide_signed_flow"
    assert ranked[-1]["feasible"] is False


def test_the_ranking_disclaims_any_economic_meaning():
    from app.cli.mbo_stage2 import FEATURE_NAMES

    result = recommend(
        l3_state=mbo_state_feature_feasibility(FEATURE_NAMES),
        options=options_feasibility(coverage=None),
        stock_flow=stock_flow_feasibility(trade_flow_coverage=None, quote_coverage=None),
        supply=[event_supply_adequacy(_supply(168, 20))],
        overlap={},
    )
    assert "No economic outcome was computed" in result["ranking_note"]


# ---------------------------------------------------------------------------
# Cross-source overlap
# ---------------------------------------------------------------------------


def test_a_window_holding_no_options_reports_them_absent():
    coverages = [
        _coverage("intraday_news_articles", rows=259, window="certified_l3_2025_06"),
        _coverage(
            "intraday_option_chain_snapshots", rows=0, window="certified_l3_2025_06"
        ),
    ]
    overlap = cross_source_overlap(coverages)
    entry = overlap["certified_l3_2025_06"]
    assert entry["sources_present"] == ["intraday_news_articles"]
    assert entry["sources_absent"] == ["intraday_option_chain_snapshots"]
    assert entry["news_and_options_coexist"] is False


def test_the_join_resolution_is_the_coarsest_present_clock():
    coverages = [
        _coverage("intraday_news_articles", rows=100, window="w"),
        _coverage("intraday_option_chain_snapshots", rows=100, window="w"),
    ]
    overlap = cross_source_overlap(coverages)
    # News is whole-second; options are microsecond. The join is whole-second.
    assert overlap["w"]["join_resolution_ns"] == SECOND_NS
    assert overlap["w"]["news_and_options_coexist"] is True


# ---------------------------------------------------------------------------
# Outcome blindness
# ---------------------------------------------------------------------------


def test_the_output_filter_removes_outcome_bearing_keys():
    from app.cli.stage40_audit import _strip_outcomes

    payload = {
        "usable_events": 168,
        "mean_net_return_bps": -1.37,
        "nested": {"clustered_t": -0.45, "rows": 10},
        "records": [{"pnl": 1.0, "symbol": "AAPL"}],
    }
    assert _strip_outcomes(payload) == {
        "usable_events": 168,
        "nested": {"rows": 10},
        "records": [{"symbol": "AAPL"}],
    }


def test_the_filter_keeps_the_audit_own_vocabulary():
    from app.cli.stage40_audit import _strip_outcomes

    payload = {"returned_rows": 5, "future_data_acquisition": ["TAQ"]}
    assert _strip_outcomes(payload) == payload


def test_no_audit_function_computes_a_forward_quantity():
    """Structural, not textual. The prose here legitimately explains why a
    forward return is forbidden; a text search would match that explanation and
    prove nothing. This inspects executable code with docstrings stripped."""
    from app.cli import stage40_audit as cli
    from app.services import stage40_audit as service

    banned_names = {
        "forward_return",
        "future_return",
        "realized_return",
        "net_return",
        "pnl",
        "compute_return",
        "holding_period_return",
    }
    for module in (service, cli):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert node.name not in banned_names, f"{module.__name__}.{node.name}"
            if isinstance(node, ast.Call):
                target = getattr(node.func, "attr", None) or getattr(node.func, "id", "")
                assert str(target) not in banned_names, target


def test_the_audit_never_imports_an_execution_or_pricing_module():
    """Stage 3.x priced fills. Nothing here may reach that machinery."""
    from app.cli import stage40_audit as cli
    from app.services import stage40_audit as service
    from app.services import stage40_audit_plan as plan_module

    banned = ("mbo_stage3_executor", "mbo_stage35_executor", "mbo_stage36_executor",
              "alpaca", "broker")
    for module in (plan_module, service, cli):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for token in banned:
                    assert token not in name.lower(), f"{module.__name__}: {name}"


def test_no_sql_in_the_audit_selects_a_price_after_an_instant():
    """The queries measure coverage and counts. None of them reads a price."""
    from app.services import stage40_audit as service

    tree = ast.parse(inspect.getsource(service))
    statements = [
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "select" in node.value.lower()
    ]
    assert statements, "expected the audit to contain SQL"
    for sql in statements:
        for banned in ("close", "midpoint", "trade_price", "last_price"):
            assert banned not in sql, f"audit SQL reads a price: {banned}"


def test_the_audit_declares_no_horizon_or_holding_period():
    """There is no horizon here to shop for, because nothing is held."""
    from app.cli import stage40_audit as cli
    from app.services import stage40_audit as service

    for module in (service, cli):
        tree = ast.parse(inspect.getsource(module))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {
            node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)
        }
        for banned in ("horizon", "holding_ns", "exit_ns", "hold_seconds"):
            assert banned not in names, f"{module.__name__}: {banned}"


def test_the_cli_exposes_no_run_command():
    """Stage 3.6 gated its run because running it spent a trial. This stage has
    nothing to gate, and a 'run' verb would imply otherwise."""
    from app.cli.stage40_audit import build_parser

    parser = build_parser()
    actions = [
        action
        for action in parser._actions
        if isinstance(action, type(parser._subparsers._group_actions[0]))
    ] if parser._subparsers else []
    choices = set()
    for action in actions:
        choices |= set(action.choices)
    assert choices == {"plan", "timestamps", "semantics", "audit"}
    assert "run" not in choices


def test_the_governance_block_appears_on_every_artifact():
    from app.cli.stage40_audit import _governance

    block = _governance()
    assert block["contains_strategy_outcome"] is False
    assert block["contains_post_decision_return"] is False
    assert block["contains_pnl"] is False
    assert block["effective_trials_before"] == 531
    assert block["effective_trials_after"] == 531
    assert block["authorizes_paper_or_live"] is False


def test_the_governance_block_survives_the_output_filter():
    """The filter must not strip the very flags that prove blindness."""
    from app.cli.stage40_audit import _governance, _strip_outcomes

    block = _governance()
    filtered = _strip_outcomes(block)
    for key in (
        "contains_strategy_outcome",
        "contains_pnl",
        "effective_trials_before",
        "effective_trials_after",
    ):
        assert key in filtered, key


# ---------------------------------------------------------------------------
# Database-free commands
# ---------------------------------------------------------------------------


def test_the_timestamp_command_runs_without_a_database():
    from app.cli.stage40_audit import timestamps

    payload = timestamps(argparse_namespace())
    assert payload["clocks_declared"] == len(TIMESTAMP_REGISTRY)
    assert payload["clocks_refused_at_decision_time"]
    assert payload["coarsest_safe_resolution_ns"] >= SECOND_NS


def test_the_semantics_command_runs_without_a_database():
    from app.cli.stage40_audit import semantics

    payload = semantics(argparse_namespace())
    assert payload["misnamed_features_not_to_use"] == list(MISNAMED_OPTION_FEATURES)
    assert "volume" in payload["option_fields_absent"]
    assert payload["l3_state_feasibility"]["vocabulary_size"] == 59


def test_the_plan_command_writes_its_artifact(tmp_path):
    from app.cli.stage40_audit import plan as plan_command

    payload = plan_command(argparse_namespace(output_dir=str(tmp_path)))
    written = tmp_path / "stage40_plan.json"
    assert written.is_file()
    assert payload["effective_trials_after"] == 531


def test_the_manifest_hashes_every_artifact(tmp_path):
    from app.services.stage40_audit import build_manifest, write_report

    write_report({"a": 1}, tmp_path / "one.json")
    write_report({"b": 2}, tmp_path / "two.json")
    manifest = build_manifest(tmp_path, artifacts=("one.json", "two.json"))
    assert [f["name"] for f in manifest["files"]] == ["one.json", "two.json"]
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])
    assert manifest["effective_trials_before"] == manifest["effective_trials_after"]


def test_a_missing_artifact_refuses_the_manifest(tmp_path):
    from app.services.stage40_audit import build_manifest

    with pytest.raises(ValueError, match="was not written"):
        build_manifest(tmp_path, artifacts=("absent.json",))


# ---------------------------------------------------------------------------
# Coverage calculations
# ---------------------------------------------------------------------------


class _FakeCursor:
    """A cursor that returns declared rows, so coverage maths is testable."""

    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple[str, list]] = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), list(params or [])))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def test_table_coverage_reads_the_declared_clock_and_window():
    from app.services.stage40_audit import measure_table_coverage

    cursor = _FakeCursor(
        [
            {
                "rows": 1234,
                "first_instant": datetime(2025, 6, 2, 13, 30, tzinfo=UTC),
                "last_instant": datetime(2025, 6, 30, 20, 0, tzinfo=UTC),
                "distinct_symbols": 8,
                "distinct_days": 20,
            }
        ]
    )
    coverage = measure_table_coverage(
        cursor,
        table="intraday_news_articles",
        clock="known_at",
        symbol_column="symbol",
        window=CERTIFIED_L3_WINDOW,
    )
    assert coverage.rows == 1234
    assert coverage.distinct_days == 20
    assert coverage.is_empty is False

    sql, params = cursor.executed[0]
    assert "known_at >= %s" in sql and "known_at < %s" in sql
    # Half-open and inclusive of the final day: 06-30 data must be counted.
    assert params[0].startswith("2025-06-02")
    assert params[1].startswith("2025-07-01")
    assert params[2] == list(CERTIFIED_L3_WINDOW.symbols)


def test_coverage_refuses_an_undeclared_clock():
    from app.services.stage40_audit import measure_table_coverage

    with pytest.raises(ValueError, match="no declared timestamp semantics"):
        measure_table_coverage(
            _FakeCursor([]),
            table="intraday_news_articles",
            clock="made_up_column",
            symbol_column="symbol",
            window=CERTIFIED_L3_WINDOW,
        )


def test_coverage_refuses_a_backfill_clock():
    from app.services.stage40_audit import measure_table_coverage

    with pytest.raises(ValueError, match="not safe at decision time"):
        measure_table_coverage(
            _FakeCursor([]),
            table="intraday_news_articles",
            clock="received_at",
            symbol_column="symbol",
            window=CERTIFIED_L3_WINDOW,
        )


def test_an_open_ended_window_has_no_upper_truncation():
    from app.services.stage40_audit import measure_table_coverage

    cursor = _FakeCursor(
        [
            {
                "rows": 0,
                "first_instant": None,
                "last_instant": None,
                "distinct_symbols": 0,
                "distinct_days": 0,
            }
        ]
    )
    coverage = measure_table_coverage(
        cursor,
        table="intraday_option_chain_snapshots",
        clock="observed_at",
        symbol_column="underlying_symbol",
        window=OPTIONS_COLLECTION_WINDOW,
    )
    assert coverage.is_empty
    _sql, params = cursor.executed[0]
    assert params[1].startswith("9999")


def test_an_empty_result_is_zero_not_an_error():
    from app.services.stage40_audit import measure_table_coverage

    coverage = measure_table_coverage(
        _FakeCursor([]),
        table="candles",
        clock="timestamp",
        symbol_column="symbol",
        window=CERTIFIED_L3_WINDOW,
    )
    assert coverage.rows == 0
    assert coverage.first_instant is None


def test_inventory_marks_an_absent_table_as_absent():
    from app.services.stage40_audit import inventory_columns

    cursor = _FakeCursor(
        [
            {
                "table_name": "candles",
                "column_name": "symbol",
                "data_type": "text",
                "is_nullable": "NO",
            }
        ]
    )
    catalogue = inventory_columns(cursor, ["candles", "a_table_that_is_gone"])
    assert catalogue["candles"]["present"] is True
    assert catalogue["candles"]["columns"][0]["nullable"] is False
    assert catalogue["a_table_that_is_gone"]["present"] is False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _coverage(table: str, *, rows: int, window: str = "w") -> TableCoverage:
    return TableCoverage(
        table=table,
        window=window,
        rows=rows,
        first_instant="2025-06-02T13:30:00+00:00" if rows else None,
        last_instant="2025-06-30T20:00:00+00:00" if rows else None,
        distinct_symbols=8 if rows else 0,
        distinct_days=20 if rows else 0,
    )


def _option_coverage(*, rows: int) -> TableCoverage:
    return _coverage("intraday_option_chain_snapshots", rows=rows)


def argparse_namespace(**kwargs):
    import argparse

    defaults = {"output_dir": ".", "command": "test", "quiet_minutes": 60}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_an_options_only_feasible_set_recommends_the_options_stock_design():
    """The branch that reaches RECOMMEND_OPTIONS_STOCK. Ruff caught this name
    was never imported, which means nothing had exercised this path."""
    from app.services.stage40_audit_plan import RECOMMEND_OPTIONS_STOCK

    # No L3 state constructible would short-circuit to insufficient_causal, so
    # give a vocabulary that backs options but leaves the L3 concepts partial.
    l3_state = mbo_state_feature_feasibility(["spread", "spread_bps"])
    l3_state["constructible_now"] = ["something"]  # non-empty, but not L3 vacuum

    result = recommend(
        l3_state=l3_state,
        options=options_feasibility(coverage=_option_coverage(rows=5000)),
        stock_flow=stock_flow_feasibility(trade_flow_coverage=None, quote_coverage=None),
        supply=[event_supply_adequacy(_supply(168, 20))],
        overlap={},
    )
    assert result["recommendation"] == RECOMMEND_OPTIONS_STOCK
    assert result["recommendation"] in RECOMMENDATIONS


def test_an_unreachable_database_refuses_by_name_not_by_traceback():
    """A detached container writing to a log file is exactly where a raw stack
    trace costs most: it makes an unreachable database look like a defect."""
    from app.cli.stage40_audit import _open_connection

    def refuse():
        raise ConnectionError("connection timeout expired")

    with pytest.raises(ValueError, match="cannot reach the database"):
        _open_connection(refuse)


def test_a_reachable_database_is_returned_unchanged():
    from app.cli.stage40_audit import _open_connection

    sentinel = object()
    assert _open_connection(lambda: sentinel) is sentinel


# ---------------------------------------------------------------------------
# Audit accounting: days, sessions, and temporal L3 coverage
# ---------------------------------------------------------------------------
#
# Three defects fixed here, all of them accounting rather than economics.
# The certified window reported 29 "sessions" for a 20-session dataset because
# the counter was fed raw news days; L3 coverage was a calendar-date test that
# admitted overnight stories; and the options window's own name collided with
# the outcome filter and deleted its section from the report.


def _coverage_for(symbol_days, *, first_hour=13, last_hour=20):
    """Certified L3 bounds covering one regular session per symbol-day."""
    from app.services.stage40_audit import L3Coverage

    spans = {}
    for symbol, day in symbol_days:
        base = datetime.fromisoformat(day).replace(tzinfo=UTC)
        first = base.replace(hour=first_hour, minute=30)
        last = base.replace(hour=last_hour, minute=0)
        spans[(symbol, day)] = (_ns(first), _ns(last))
    return L3Coverage(
        spans=spans,
        cadences=("50ev", "200ev"),
        symbol_days_resolved=len(spans),
        symbol_days_missing=(),
    )


def _ns(moment) -> int:
    from app.services.stage40_audit import _epoch_nanoseconds

    return _epoch_nanoseconds(moment)


def test_raw_news_days_are_counted_separately_from_covered_sessions():
    """The exact defect: raw news days inflated the reported session count.

    Stories here land on four calendar days, only two of which hold certified
    L3 coverage, and one of those stories is outside session hours.
    """
    rows = [
        _story("AAPL", datetime(2025, 6, 2, 14, 0, tzinfo=UTC)),  # covered
        _story("AAPL", datetime(2025, 6, 3, 14, 0, tzinfo=UTC)),  # covered
        _story("AAPL", datetime(2025, 6, 4, 2, 0, tzinfo=UTC)),  # overnight
        _story("AAPL", datetime(2025, 6, 7, 15, 0, tzinfo=UTC)),  # weekend
    ]
    supply = summarise_event_supply(
        rows,
        window="w",
        quiet_minutes=60,
        l3_coverage=_coverage_for(
            [("AAPL", "2025-06-02"), ("AAPL", "2025-06-03"), ("AAPL", "2025-06-04")]
        ),
    )
    assert supply.raw_events == 4
    assert supply.isolated_events == 4
    assert supply.raw_distinct_days == 4
    assert supply.isolated_distinct_days == 4
    # Only the two in-session events count, on two sessions.
    assert supply.with_l3_coverage == 2
    assert supply.l3_covered_distinct_sessions == 2
    assert supply.l3_covered_distinct_sessions < supply.raw_distinct_days


def test_covered_sessions_can_never_exceed_the_frozen_certified_set():
    """However much news arrives, coverage cannot invent a session.

    Twenty certified symbol-days, and news on forty calendar days including
    weekends. The covered-session count is bounded by the certified set.
    """
    certified_days = [f"2025-06-{day:02d}" for day in range(2, 22)]
    coverage = _coverage_for([("AAPL", day) for day in certified_days])

    rows = []
    for day in range(1, 31):
        moment = datetime(2025, 6, day, 14, 0, tzinfo=UTC)
        rows.append(_story("AAPL", moment))
    supply = summarise_event_supply(
        rows, window="w", quiet_minutes=60, l3_coverage=coverage
    )

    assert supply.raw_distinct_days == 30
    assert supply.l3_covered_distinct_sessions <= len(certified_days)
    assert supply.l3_covered_distinct_sessions == 20
    assert supply.with_l3_coverage <= supply.isolated_events


def test_the_real_certified_session_set_bounds_the_count():
    """Against the frozen census itself, not a constructed list."""
    from app.cli.stage40_audit import _certified_sessions

    certified = _certified_sessions()
    assert len(certified) == 20

    coverage = _coverage_for([("AAPL", day) for day in certified])
    rows = [
        _story("AAPL", datetime.fromisoformat(day).replace(hour=14, tzinfo=UTC))
        for day in certified
    ]
    # Plus a story on a date that is not a certified session at all.
    rows.append(_story("AAPL", datetime(2025, 7, 15, 14, 0, tzinfo=UTC)))
    rows.sort(key=lambda r: r["known_at"])

    supply = summarise_event_supply(
        rows, window="w", quiet_minutes=60, l3_coverage=coverage
    )
    assert supply.l3_covered_distinct_sessions == 20
    assert supply.raw_distinct_days == 21


@pytest.mark.parametrize(
    ("hour", "minute", "covered"),
    [
        (2, 0, False),  # overnight, before the tape
        (13, 29, False),  # one minute before first availability
        (13, 30, True),  # exactly the first certified instant
        (16, 0, True),  # mid-session
        (20, 0, True),  # exactly the last certified instant
        (20, 1, False),  # one minute after the tape stops
        (23, 30, False),  # after hours
    ],
)
def test_l3_coverage_is_temporal_not_calendar(hour, minute, covered):
    """A certified date is not a certified instant.

    Every case here falls on the SAME certified session date. Only the ones
    inside actual availability count.
    """
    moment = datetime(2025, 6, 2, hour, minute, tzinfo=UTC)
    supply = summarise_event_supply(
        [_story("AAPL", moment)],
        window="w",
        quiet_minutes=60,
        l3_coverage=_coverage_for([("AAPL", "2025-06-02")]),
    )
    assert supply.isolated_events == 1
    assert supply.with_l3_coverage == (1 if covered else 0)
    assert supply.l3_covered_distinct_sessions == (1 if covered else 0)


def test_a_different_symbol_on_a_covered_day_is_not_covered():
    """Coverage is per symbol-session, not per session."""
    coverage = _coverage_for([("AAPL", "2025-06-02")])
    supply = summarise_event_supply(
        [_story("MSFT", datetime(2025, 6, 2, 14, 0, tzinfo=UTC))],
        window="w",
        quiet_minutes=60,
        l3_coverage=coverage,
    )
    assert supply.isolated_events == 1
    assert supply.with_l3_coverage == 0


def test_absent_coverage_fails_closed_to_zero():
    """No coverage information means no coverage claim."""
    rows = [_story("AAPL", datetime(2025, 6, 2, 14, 0, tzinfo=UTC))]
    supply = summarise_event_supply(rows, window="w", quiet_minutes=60)
    assert supply.isolated_events == 1
    assert supply.with_l3_coverage == 0
    assert supply.l3_covered_distinct_sessions == 0


def test_the_adequacy_gate_reads_covered_sessions_not_raw_days():
    """The gate must not be satisfied by weekends and overnight stories."""
    supply = EventSupply(
        window="w",
        raw_events=900,
        isolated_events=400,
        with_l3_coverage=168,
        with_option_observation=0,
        with_all_sources=0,
        raw_distinct_symbols=8,
        raw_distinct_days=29,  # the number that wrongly passed the gate
        isolated_distinct_days=25,
        l3_covered_distinct_sessions=12,  # the number that should decide it
        option_covered_distinct_sessions=0,
        all_source_distinct_sessions=0,
        by_hour_utc={},
    )
    verdict = event_supply_adequacy(supply)
    assert verdict["l3_covered_distinct_sessions"] == 12
    assert verdict["meets_event_floor"] is True
    assert verdict["meets_session_floor"] is False
    assert verdict["adequate"] is False
    # The raw counts are reported, but they do not decide anything.
    assert verdict["raw_distinct_days"] == 29


def test_the_adequacy_gate_passes_on_covered_sessions():
    supply = EventSupply(
        window="w",
        raw_events=900,
        isolated_events=400,
        with_l3_coverage=168,
        with_option_observation=0,
        with_all_sources=0,
        raw_distinct_symbols=8,
        raw_distinct_days=29,
        isolated_distinct_days=25,
        l3_covered_distinct_sessions=20,
        option_covered_distinct_sessions=0,
        all_source_distinct_sessions=0,
        by_hour_utc={},
    )
    verdict = event_supply_adequacy(supply)
    assert verdict["meets_session_floor"] is True
    assert verdict["adequate"] is True


def test_the_declared_floors_are_unchanged():
    """The defect was accounting. The thresholds must not have moved."""
    assert MIN_EVENTS_FOR_MECHANISM == 100
    assert MIN_SESSIONS_FOR_MECHANISM == 15
    from app.cli.stage40_audit import DEFAULT_QUIET_MINUTES

    assert DEFAULT_QUIET_MINUTES == 60


# --- coverage loading from the frozen feature files ------------------------


def _write_feature_file(root, cadence, stem, instants):
    import pyarrow as pa
    import pyarrow.parquet as pq

    directory = root / cadence
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"feature_available_ts_recv": pa.array(instants, pa.int64())}),
        directory / f"{stem}.{cadence}.parquet",
    )


def test_coverage_is_read_from_the_frozen_feature_files(tmp_path):
    from app.services.stage40_audit import load_l3_coverage

    _write_feature_file(tmp_path, "50ev", "AAPL_2025-06-02", [1000, 5000, 9000])
    _write_feature_file(tmp_path, "200ev", "AAPL_2025-06-02", [2000, 6000, 8000])

    coverage = load_l3_coverage(
        tmp_path, symbols=["AAPL"], sessions=["2025-06-02"]
    )
    # Intersection: the later first instant and the earlier last instant.
    assert coverage.spans[("AAPL", "2025-06-02")] == (2000, 8000)
    assert coverage.covers("AAPL", "2025-06-02", 2000) is True
    assert coverage.covers("AAPL", "2025-06-02", 8000) is True
    assert coverage.covers("AAPL", "2025-06-02", 1999) is False
    assert coverage.covers("AAPL", "2025-06-02", 8001) is False
    assert coverage.symbol_days_resolved == 1


def test_a_symbol_day_missing_a_cadence_contributes_no_span(tmp_path):
    """Fail closed. A guessed bound would admit events into the population."""
    from app.services.stage40_audit import load_l3_coverage

    _write_feature_file(tmp_path, "50ev", "AAPL_2025-06-02", [1000, 9000])
    # No 200ev file for this symbol-day.
    coverage = load_l3_coverage(tmp_path, symbols=["AAPL"], sessions=["2025-06-02"])
    assert coverage.spans == {}
    assert coverage.symbol_days_missing == ("AAPL_2025-06-02",)
    assert coverage.covers("AAPL", "2025-06-02", 5000) is False


def test_an_entirely_absent_feature_directory_yields_no_coverage(tmp_path):
    from app.services.stage40_audit import load_l3_coverage

    coverage = load_l3_coverage(
        tmp_path / "nothing-here", symbols=["AAPL"], sessions=["2025-06-02"]
    )
    assert coverage.spans == {}
    assert coverage.sessions == set()


def test_the_audit_refuses_without_a_feature_directory():
    """The only alternative would be the calendar assumption this removes."""
    import argparse as _argparse

    from app.cli.stage40_audit import _l3_coverage

    with pytest.raises(ValueError, match="--features-dir is required"):
        _l3_coverage(_argparse.Namespace(features_dir=None), CERTIFIED_L3_WINDOW)

    with pytest.raises(ValueError, match="feature directory is missing"):
        _l3_coverage(
            _argparse.Namespace(features_dir="/no/such/place"), CERTIFIED_L3_WINDOW
        )


def test_the_features_dir_argument_is_required_by_the_parser():
    from app.cli.stage40_audit import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["audit"])
    args = parser.parse_args(["audit", "--features-dir", "/tmp/x"])
    assert args.features_dir == "/tmp/x"


def test_epoch_nanoseconds_is_exact_integer_arithmetic():
    """A float second count cannot represent a nanosecond instant."""
    from app.services.stage40_audit import _epoch_nanoseconds

    moment = datetime(2025, 6, 2, 13, 59, 45, 494493, tzinfo=UTC)
    assert _epoch_nanoseconds(moment) == 1_748_872_785_494_493_000
    assert isinstance(_epoch_nanoseconds(moment), int)


# --- the options window must survive output stripping ---------------------


def test_the_options_window_name_cannot_collide_with_the_outcome_filter():
    """The original name contained "forward" and was stripped wholesale."""
    from app.cli.stage40_audit import _strip_outcomes

    assert OPTIONS_COLLECTION_WINDOW.name == "options_2026_collection_window"
    assert "forward" not in OPTIONS_COLLECTION_WINDOW.name

    payload = {OPTIONS_COLLECTION_WINDOW.name: {"rows": 5_000_000}}
    assert _strip_outcomes(payload) == payload


def test_options_window_evidence_survives_stripping_end_to_end():
    """A realistic per-window report section, filtered."""
    from app.cli.stage40_audit import _strip_outcomes

    report = {
        "per_window": {
            "certified_l3_2025_06": {
                "event_supply": {"l3_covered_distinct_sessions": 20},
            },
            "options_2026_collection_window": {
                "event_supply": {
                    "raw_events": 4000,
                    "option_covered_distinct_sessions": 5,
                },
                "options_feasibility": {"verdict": "options_cross_market_state_only"},
                "option_quality": {"crossed_or_locked_rows": 3},
            },
        }
    }
    clean = _strip_outcomes(report)
    section = clean["per_window"]["options_2026_collection_window"]
    assert section["options_feasibility"]["verdict"] == "options_cross_market_state_only"
    assert section["event_supply"]["option_covered_distinct_sessions"] == 5
    assert section["option_quality"]["crossed_or_locked_rows"] == 3


def test_genuine_forward_return_keys_are_still_stripped():
    """Renaming the innocent window must not weaken the prohibition."""
    from app.cli.stage40_audit import _strip_outcomes

    payload = {
        "forward_return_bps": 8.0,
        "forward_returns": [1.0, 2.0],
        "future_price": 100.0,
        "mean_net_return_bps": -1.37,
        "realized_pnl": 42.0,
        "clustered_t": -0.45,
        "holding_period_minutes": 5,
        "keep_me": True,
    }
    assert _strip_outcomes(payload) == {"keep_me": True}


def test_no_broad_forward_exemption_was_added():
    """Only specific, non-economic names are exempt -- not "forward" itself."""
    from app.services.stage40_audit_plan import (
        OUTCOME_BEARING_TOKENS,
        OUTCOME_TOKEN_EXEMPTIONS,
    )

    assert "forward" in OUTCOME_BEARING_TOKENS
    assert "future" in OUTCOME_BEARING_TOKENS
    for exemption in OUTCOME_TOKEN_EXEMPTIONS:
        assert exemption not in ("forward", "future", "return", "pnl")
    # And the window name is not exempted -- it simply no longer collides.
    assert OPTIONS_COLLECTION_WINDOW.name not in OUTCOME_TOKEN_EXEMPTIONS


def test_the_ledger_still_reads_531_both_sides_after_the_fix():
    from app.cli.stage40_audit import _governance

    block = _governance()
    assert block["effective_trials_before"] == 531
    assert block["effective_trials_after"] == 531
    assert block["contains_strategy_outcome"] is False
    assert block["contains_post_decision_return"] is False
    assert block["contains_pnl"] is False


def test_the_coverage_loader_reads_no_price_column():
    """It opens feature files, so it must be explicit about what it reads."""
    import ast as _ast
    import inspect as _inspect

    from app.services import stage40_audit as service

    source = _inspect.getsource(service.load_l3_coverage)
    tree = _ast.parse(_inspect.cleandoc(source).replace("def load_l3_coverage", "def f", 1))
    literals = {
        node.value
        for node in _ast.walk(tree)
        if isinstance(node, _ast.Constant) and isinstance(node.value, str)
    }
    for banned in ("midpoint", "close", "trade_price", "best_bid_price"):
        assert banned not in literals


def test_the_cli_repo_root_points_at_the_repository():
    """It was one level too shallow, so the frozen census could not be found
    and the default output directory pointed into apps/."""
    from app.cli.mbo_stage36 import REPO_ROOT as STAGE36_ROOT
    from app.cli.stage40_audit import DEFAULT_OUTPUT_DIR, REPO_ROOT

    assert REPO_ROOT == STAGE36_ROOT
    assert (REPO_ROOT / "reports" / "tier1_stage36_preoutcome" / "v1").is_dir()
    assert REPO_ROOT.name != "apps"
    assert DEFAULT_OUTPUT_DIR.parent.parent == REPO_ROOT / "reports"
