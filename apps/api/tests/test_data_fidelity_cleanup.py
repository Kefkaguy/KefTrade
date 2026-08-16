"""Stage 0 cleanup: retirement, nanosecond fidelity, and quote accounting.

These pin the *consequences* of the Stage 0 result, not the result itself.
"""

from __future__ import annotations

import pytest

from app.providers.alpaca import (
    QUOTE_REJECT_CROSSED,
    QUOTE_REJECT_NEGATIVE_SIZE,
    QUOTE_REJECT_NONPOSITIVE_PRICE,
    QUOTE_REJECT_UNPARSEABLE,
    QuoteNormalizationCounters,
    normalize_stock_quote,
    normalize_stock_quotes,
    parse_rfc3339_nanoseconds,
)
from app.services.data_fidelity import (
    PRESERVED_QUOTE_PRICE_FIELDS,
    RETIRED_QUOTE_QUEUE_FIELDS,
    RETIREMENT_CODE,
    RETIREMENT_REASON,
    architecture_retirement,
    assert_architecture_runnable,
    factor_retirement,
    retired_fields_present,
)

# ---------------------------------------------------------------------------
# The retirement record
# ---------------------------------------------------------------------------


def test_retirement_reason_states_the_measurement_and_the_rule():
    assert RETIREMENT_CODE == "retired_data_fidelity"
    assert "45.224%" in RETIREMENT_REASON
    assert "30%" in RETIREMENT_REASON


def test_retirement_covers_queue_fields_and_spares_price_fields():
    """The finding is about quoted sizes. Spread is computed from prices."""
    assert "normalized_order_flow_imbalance" in RETIRED_QUOTE_QUEUE_FIELDS
    assert "order_flow_imbalance" in RETIRED_QUOTE_QUEUE_FIELDS
    # Depth is the mean of the two quoted sizes, so it inherits the defect.
    assert "mean_depth" in RETIRED_QUOTE_QUEUE_FIELDS
    # Spread survives: NBBO prices are real even when the sizes beside them are
    # one venue's fragment.
    assert "median_spread_bps" in PRESERVED_QUOTE_PRICE_FIELDS
    assert not (RETIRED_QUOTE_QUEUE_FIELDS & PRESERVED_QUOTE_PRICE_FIELDS)


def test_retired_fields_present_identifies_only_the_retired_ones():
    assert retired_fields_present(
        ["median_spread_bps", "mean_depth", "session_vwap", "order_flow_imbalance"]
    ) == ["mean_depth", "order_flow_imbalance"]


# ---------------------------------------------------------------------------
# Families and factors refuse, and say why
# ---------------------------------------------------------------------------


def test_retired_v2_family_refuses_to_generate_candidates():
    from app.services.labs.intraday.families.v2 import (
        families,  # noqa: F401  (registers)
    )
    from app.services.labs.intraday.families.v2.base import generate_v2_candidates

    with pytest.raises(ValueError) as excinfo:
        generate_v2_candidates("liquidity_shock_reversal_v1")
    assert RETIREMENT_CODE in str(excinfo.value)
    assert "45.224%" in str(excinfo.value)


def test_retired_v2_family_refuses_to_construct():
    from app.services.labs.intraday.families.v2 import (
        families,  # noqa: F401  (registers)
    )
    from app.services.labs.intraday.families.v2.base import V2_FAMILIES

    with pytest.raises(ValueError) as excinfo:
        V2_FAMILIES["liquidity_shock_reversal_v1"]({"direction": "long"}, timeframe="30m")
    assert RETIREMENT_CODE in str(excinfo.value)


def test_retired_family_is_preserved_not_deleted():
    """Evidence and code stay readable; only execution is blocked."""
    from app.services.labs.intraday.families.registry import FAMILY_REGISTRY
    from app.services.labs.intraday.families.v2.base import V2_FAMILIES, V2_HYPOTHESES

    assert "liquidity_shock_reversal_v1" in V2_FAMILIES
    assert "liquidity_shock_reversal_v1" in V2_HYPOTHESES
    definition = FAMILY_REGISTRY["liquidity_shock_reversal_v1"]
    assert definition.status == RETIREMENT_CODE
    assert definition.name  # the hypothesis title still resolves


def test_healthy_families_are_untouched():
    from app.services.labs.intraday.families.v2 import (
        families,  # noqa: F401  (registers)
    )
    from app.services.labs.intraday.families.v2.base import generate_v2_candidates
    from app.services.labs.intraday.families.v2.families import V2_ARCHITECTURES

    survivors = [a for a in V2_ARCHITECTURES if a != "liquidity_shock_reversal_v1"]
    assert survivors, "the retirement must not empty the registry"
    assert generate_v2_candidates(survivors[0], max_candidates=1)


def test_factor_retirement_targets_only_the_quote_queue_factor():
    assert factor_retirement("liquidity_shock_reversal")["status"] == RETIREMENT_CODE
    # Trade-signed factors survived Stage 0 (Lee-Ready vs tick rule agreed on
    # 88-90% of trades), so they must not be retired by association.
    assert factor_retirement("signed_trade_imbalance_continuation_1bar") is None
    assert factor_retirement("auction_imbalance_pressure") is None
    assert architecture_retirement("gap_fill") is None


def test_assert_architecture_runnable_passes_healthy_architectures():
    assert_architecture_runnable("gap_fill")  # does not raise


def test_retirement_record_carries_the_evidence_pointer():
    record = factor_retirement("liquidity_shock_reversal")
    assert record["measured_rotation_share"] == 0.45224
    assert record["allowed_rotation_share"] == 0.30
    assert record["quotes_measured"] == 23_016_760
    assert record["report"].endswith("stage0-microstructure-probe-results.md")


# ---------------------------------------------------------------------------
# Nanosecond fidelity
# ---------------------------------------------------------------------------


def test_normalized_quote_carries_full_source_precision():
    quote = normalize_stock_quote(
        "INTC", {"t": "2026-06-01T13:30:00.123456789Z", "bp": 10.0, "ap": 10.01, "bs": 1, "as": 2}
    )
    assert quote["timestamp_ns"] % 1_000_000_000 == 123_456_789
    # The datetime is still microsecond-floored -- that is the point.
    assert quote["timestamp"].microsecond == 123_456


def test_two_updates_in_one_microsecond_stay_distinct():
    """The exact collapse migration 080 fixes."""
    first = normalize_stock_quote(
        "INTC", {"t": "2026-06-01T13:30:00.123456111Z", "bp": 10.0, "ap": 10.01, "bs": 1, "as": 2}
    )
    second = normalize_stock_quote(
        "INTC", {"t": "2026-06-01T13:30:00.123456999Z", "bp": 10.0, "ap": 10.01, "bs": 9, "as": 2}
    )
    assert first["timestamp"] == second["timestamp"]  # collide at microsecond
    assert first["timestamp_ns"] != second["timestamp_ns"]  # distinct at source


def test_nanosecond_parser_is_shared_with_the_probe():
    """One implementation, so the probe and ingestion cannot disagree."""
    from app.services import microstructure_probe

    assert microstructure_probe.parse_rfc3339_nanoseconds is parse_rfc3339_nanoseconds
    assert parse_rfc3339_nanoseconds("2026-06-01T13:30:00Z") % 1_000_000_000 == 0


# ---------------------------------------------------------------------------
# Quote rejection accounting
# ---------------------------------------------------------------------------


def test_every_rejection_is_attributed_to_a_named_cause():
    counters = QuoteNormalizationCounters()
    rows = [
        {"t": "2026-06-01T13:30:00Z", "bp": 10.0, "ap": 10.01, "bs": 1, "as": 2},  # good
        {"t": "not-a-time", "bp": 10.0, "ap": 10.01, "bs": 1, "as": 2},
        {"t": "2026-06-01T13:30:01Z", "bp": 0.0, "ap": 10.01, "bs": 1, "as": 2},
        {"t": "2026-06-01T13:30:02Z", "bp": 10.02, "ap": 10.01, "bs": 1, "as": 2},  # crossed
        {"t": "2026-06-01T13:30:03Z", "bp": 10.0, "ap": 10.01, "bs": -1, "as": 2},
    ]
    for row in rows:
        normalize_stock_quote("INTC", row, counters=counters)
    summary = counters.as_dict()
    assert summary["received"] == 5
    assert summary["accepted"] == 1
    assert summary["rejected"] == 4
    assert summary[QUOTE_REJECT_UNPARSEABLE] == 1
    assert summary[QUOTE_REJECT_NONPOSITIVE_PRICE] == 1
    assert summary[QUOTE_REJECT_CROSSED] == 1
    assert summary[QUOTE_REJECT_NEGATIVE_SIZE] == 1
    assert summary["received"] == summary["accepted"] + summary["rejected"]


def test_crossed_quotes_are_counted_so_a_halt_cannot_vanish():
    """Mass cancellation during a halt produces crossed markets; if those are
    dropped silently, a halted session looks like a quiet one."""
    rows = [{"t": f"2026-06-01T13:30:{i:02d}Z", "bp": 10.02, "ap": 10.01} for i in range(7)]
    normalized, counters = normalize_stock_quotes("INTC", rows)
    assert normalized == []
    assert counters.as_dict()[QUOTE_REJECT_CROSSED] == 7
    assert counters.as_dict()["rejection_rate"] == 1.0


def test_counters_are_optional_so_existing_callers_are_unaffected():
    quote = normalize_stock_quote(
        "INTC", {"t": "2026-06-01T13:30:00Z", "bp": 10.0, "ap": 10.01, "bs": 1, "as": 2}
    )
    assert quote is not None
    assert normalize_stock_quote("INTC", {"bad": "row"}) is None


def test_unknown_rejection_reason_is_refused():
    counters = QuoteNormalizationCounters()
    with pytest.raises(ValueError):
        counters.record_rejected("rejected_because_i_said_so")
