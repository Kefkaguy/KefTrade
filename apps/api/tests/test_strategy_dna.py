"""Phase 13.1: Strategy DNA determinism, versioning, vocabulary, backfill."""

import json

import pytest

from app.services.strategy_dna import (
    BEHAVIORAL_FIELDS,
    DNA_SCHEMA_VERSION,
    FAMILY_DNA,
    VOCABULARY,
    backfill_strategy_dna,
    behavioral_similarity,
    build_dna_payload,
    compute_fingerprint,
    register_family_dna,
)


def sample_payload(**overrides):
    payload = {
        "family_architecture": "test_family_v1",
        "strategy_version": "v1",
        "direction_support": ["long"],
        "execution_capability": "simulation_only",
        "entry_structure": "range_breakout",
        "confirmation_structure": ["relative_volume"],
        "exit_structure": ["stop_loss", "session_close_forced"],
        "holding_horizon_class": "intraday_hours",
        "timeframe_class": "intraday_15m_30m",
        "expected_frequency_class": "multiple_per_session",
        "trend_dependency": "benefits_from_trend",
        "volatility_dependency": "agnostic",
        "volume_dependency": "requires_elevated",
        "session_dependency": "first_two_hours",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["range_bound"],
        "feature_dependencies": ["opening_range_high"],
        "evidence_confidence": "untested",
    }
    payload.update(overrides)
    return payload


def test_fingerprint_is_deterministic_across_key_order():
    payload = sample_payload()
    shuffled = dict(reversed(list(payload.items())))

    assert compute_fingerprint(payload) == compute_fingerprint(shuffled)


def test_fingerprint_is_insensitive_to_list_order_but_sensitive_to_membership():
    base = sample_payload(exit_structure=["stop_loss", "session_close_forced"])
    reordered = sample_payload(exit_structure=["session_close_forced", "stop_loss"])
    different = sample_payload(exit_structure=["stop_loss"])

    assert compute_fingerprint(base) == compute_fingerprint(reordered)
    assert compute_fingerprint(base) != compute_fingerprint(different)


def test_fingerprint_changes_when_any_behavioral_field_changes():
    base_fingerprint = compute_fingerprint(sample_payload())

    assert compute_fingerprint(sample_payload(behavior_class="mean_reversion")) != base_fingerprint
    assert compute_fingerprint(sample_payload(entry_structure="range_fade")) != base_fingerprint
    assert compute_fingerprint(sample_payload(session_dependency="power_hour_only")) != base_fingerprint


def test_fingerprint_changes_with_dna_schema_version():
    payload = sample_payload()

    assert compute_fingerprint(payload, dna_schema_version=1) != compute_fingerprint(payload, dna_schema_version=2)


def test_build_dna_payload_rejects_missing_and_unknown_fields():
    incomplete = sample_payload()
    del incomplete["behavior_class"]
    with pytest.raises(ValueError, match="missing"):
        build_dna_payload(incomplete)

    extra = sample_payload()
    extra["not_a_real_field"] = "x"
    with pytest.raises(ValueError, match="unknown"):
        build_dna_payload(extra)


def test_build_dna_payload_rejects_values_outside_the_vocabulary():
    with pytest.raises(ValueError, match="outside the vocabulary"):
        build_dna_payload(sample_payload(behavior_class="vibes"))
    with pytest.raises(ValueError, match="outside the vocabulary"):
        build_dna_payload(sample_payload(exit_structure=["teleport"]))


def test_build_dna_payload_rejects_empty_list_fields():
    with pytest.raises(ValueError, match="non-empty list"):
        build_dna_payload(sample_payload(direction_support=[]))


def test_feature_dependencies_are_shape_checked_but_not_vocabulary_checked():
    payload = build_dna_payload(sample_payload(feature_dependencies=["some_new_feature_v2"]))

    assert payload["feature_dependencies"] == ["some_new_feature_v2"]


def test_behavioral_similarity_is_one_for_identical_behavior_and_ignores_identity():
    a = sample_payload(family_architecture="alpha_v1", strategy_version="v1")
    b = sample_payload(family_architecture="beta_v9", strategy_version="v9")

    assert behavioral_similarity(a, b) == 1.0


def test_behavioral_similarity_falls_when_behavior_diverges():
    a = sample_payload()
    b = sample_payload(
        behavior_class="mean_reversion",
        entry_structure="range_fade",
        trend_dependency="requires_range",
        session_dependency="power_hour_only",
    )

    similarity = behavioral_similarity(a, b)
    assert 0.0 < similarity < 1.0
    assert similarity < behavioral_similarity(a, sample_payload(behavior_class="mean_reversion"))


def test_behavioral_similarity_uses_jaccard_for_list_fields():
    a = sample_payload(exit_structure=["stop_loss", "session_close_forced"])
    b = sample_payload(exit_structure=["stop_loss", "time_stop"])

    # One field differs by Jaccard 1/3; all other behavioral fields agree.
    expected = ((len(BEHAVIORAL_FIELDS) - 1) + (1 / 3)) / len(BEHAVIORAL_FIELDS)
    assert behavioral_similarity(a, b) == round(expected, 4)


def test_every_registered_family_has_a_valid_payload_and_unique_fingerprint():
    fingerprints = {}
    for architecture, payload in FAMILY_DNA.items():
        build_dna_payload(payload)
        assert payload["family_architecture"] == architecture
        fingerprint = compute_fingerprint(payload)
        assert fingerprint not in fingerprints, f"{architecture} collides with {fingerprints.get(fingerprint)}"
        fingerprints[fingerprint] = architecture


def test_all_eight_phase_12_families_have_dna():
    expected = {
        "opening_range_breakout_v1", "vwap_reversion_v1", "gap_fill_v1", "session_momentum_v1",
        "intraday_trend_pullback_v1", "ema_trend_continuation_v1", "opening_fade_v1",
        "vwap_trend_continuation_v1",
    }
    assert expected <= set(FAMILY_DNA)


def test_archived_phase_12_families_are_labeled_with_their_real_evidence_outcome():
    # Phase 12.4 concluded all six v1 families lacked an edge; DNA must not
    # silently upgrade that to something more flattering.
    assert FAMILY_DNA["gap_fill_v1"]["evidence_confidence"] == "tested_negative_archived"
    assert FAMILY_DNA["opening_range_breakout_v1"]["evidence_confidence"] == "tested_negative_archived"
    # Session Momentum is the one real specialist lead (AMD 30m long), not an elite.
    assert FAMILY_DNA["session_momentum_v1"]["evidence_confidence"] == "specialist_lead"
    assert "validated_elite" not in {payload["evidence_confidence"] for payload in FAMILY_DNA.values()}


def test_register_family_dna_is_idempotent_but_rejects_conflicting_redefinition():
    payload = sample_payload(family_architecture="registration_test_v1")
    register_family_dna("registration_test_v1", payload)
    register_family_dna("registration_test_v1", dict(payload))  # same content: fine

    with pytest.raises(ValueError, match="different fingerprint"):
        register_family_dna("registration_test_v1", sample_payload(family_architecture="registration_test_v1", behavior_class="hybrid"))

    FAMILY_DNA.pop("registration_test_v1", None)


class FakeDnaConn:
    """Mimics the append-only table: ON CONFLICT DO NOTHING returns no row."""

    def __init__(self):
        self.rows: dict[tuple[str, str, int], dict] = {}
        self.commits = 0

    def execute(self, query, params=None):
        assert "INSERT INTO strategy_dna" in query
        architecture, version, schema_version, fingerprint, dna = params
        key = (architecture, version, schema_version)
        if key in self.rows:
            return FakeResult(None)
        self.rows[key] = {"fingerprint": fingerprint, "dna": dna.obj}
        return FakeResult({"id": len(self.rows)})

    def commit(self):
        self.commits += 1


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


def test_backfill_writes_one_row_per_family_and_is_idempotent():
    conn = FakeDnaConn()

    first = backfill_strategy_dna(conn)
    assert len(first["written"]) == len(FAMILY_DNA)
    assert first["already_present"] == []
    assert first["dna_schema_version"] == DNA_SCHEMA_VERSION

    second = backfill_strategy_dna(conn)
    assert second["written"] == []
    assert len(second["already_present"]) == len(FAMILY_DNA)
    assert len(conn.rows) == len(FAMILY_DNA)


def test_backfill_stores_canonical_payloads_that_reproduce_their_fingerprint():
    conn = FakeDnaConn()
    backfill_strategy_dna(conn)

    for (architecture, _version, _schema), row in conn.rows.items():
        assert compute_fingerprint(row["dna"]) == row["fingerprint"]
        # Canonical payload round-trips through JSON unchanged.
        assert json.loads(json.dumps(row["dna"], sort_keys=True)) == row["dna"]


def test_vocabulary_has_no_duplicate_values_within_a_field():
    for field, values in VOCABULARY.items():
        assert len(values) == len(set(values)), f"duplicate vocabulary entries in {field}"
