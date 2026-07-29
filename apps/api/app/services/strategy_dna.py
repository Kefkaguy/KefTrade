"""Phase 13.1: Strategy DNA -- versioned behavioral description of strategy families.

DNA describes *what a strategy does to the market* (entry structure,
dependencies, behavior class, regime requirements), separate from the raw
parameter grid a candidate happens to carry. Two candidates with different
momentum thresholds share one DNA; two families that both "buy strength"
but through different structures (opening-range breakout vs. relative-volume
momentum) have different DNA that is still measurably *similar* on the
behavioral axes.

Storage: the append-only `strategy_dna` table (migration 051). One row per
(family_architecture, strategy_version, dna_schema_version). Correcting a
record means appending a higher dna_schema_version, never editing.

Fingerprints are sha256 over the canonical (sorted-key, compact-separator)
JSON of identity + behavioral payload, so they are deterministic across
processes, machines, and dict insertion orders. Behavioral similarity is
computed over BEHAVIORAL_FIELDS only -- deliberately excluding identity
fields -- so "same family, new version" and "different family, same
behavior" are distinguishable. Parameter similarity remains a separate,
pre-existing concept (`research_campaigns.candidate_parameter_distance`);
nothing here conflates the two.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

DNA_SCHEMA_VERSION = 2

# Closed vocabularies. A DNA payload using a value outside these sets is
# rejected at build time -- fingerprints are only meaningful if the
# vocabulary is controlled, and vocabulary growth must be an explicit,
# reviewed change to this module (which is itself versioned by
# DNA_SCHEMA_VERSION).
VOCABULARY: dict[str, tuple[str, ...]] = {
    "direction_support": ("long", "short"),
    "execution_capability": ("simulation_only", "external_paper_long_only"),
    "entry_structure": (
        "range_breakout", "range_fade", "vwap_pullback", "vwap_extension_fade",
        "gap_open_continuation", "gap_open_fade", "volume_surge_continuation",
        "compression_breakout", "time_of_day_entry", "structure_break",
        "structure_break_failure", "momentum_bar_continuation", "vwap_deviation_fade",
        "indicator_crossover", "opening_range_extension_fade", "trend_pullback",
        "cross_sectional_rank_extreme", "opening_repricing_flow",
    ),
    "confirmation_structure": (
        "relative_volume", "vwap_alignment", "closing_confirmation", "momentum_direction",
        "volatility_expansion", "structure_state", "session_window", "range_quality",
        "declining_momentum", "none",
    ),
    "exit_structure": (
        "fixed_r_multiple_target", "vwap_target", "prior_close_target",
        "opposite_range_boundary_target", "stop_loss", "session_close_forced",
        "time_stop", "failed_signal_exit",
    ),
    "holding_horizon_class": ("intraday_minutes", "intraday_hours", "multi_day", "multi_week"),
    "timeframe_class": ("intraday_15m_30m", "intraday_30m", "intraday_1h", "swing_1h_4h", "daily"),
    "expected_frequency_class": ("multiple_per_session", "roughly_daily", "few_per_week", "sparse"),
    "trend_dependency": ("requires_trend", "requires_range", "benefits_from_trend", "agnostic"),
    "volatility_dependency": (
        "requires_compression_then_expansion", "requires_expansion",
        "requires_normal_or_low", "agnostic",
    ),
    "volume_dependency": ("requires_elevated", "requires_confirmation", "agnostic"),
    "session_dependency": (
        "open_only", "first_two_hours", "avoids_open", "midday_only",
        "power_hour_only", "any_session_time",
    ),
    "gap_dependency": ("requires_gap", "requires_no_major_gap", "agnostic"),
    "market_structure_dependency": ("requires_confirmed_structure", "uses_structure_context", "agnostic"),
    "behavior_class": ("momentum", "mean_reversion", "hybrid"),
    "required_regime": ("trending_up", "trending_down", "range_bound", "high_volatility", "normal_volatility", "low_volatility", "any"),
    "invalidation_regime": ("trending_up", "trending_down", "range_bound", "high_volatility", "normal_volatility", "low_volatility", "none_declared"),
    "evidence_confidence": ("untested", "tested_negative_archived", "specialist_lead", "tested_mixed", "validated_elite"),
}

# Fields whose values are lists drawn from the vocabulary (order-insensitive,
# canonicalized by sorting); the rest are single values.
LIST_FIELDS = (
    "direction_support", "confirmation_structure", "exit_structure",
    "required_regime", "invalidation_regime", "feature_dependencies",
)

# feature_dependencies is free-form-ish (names of feature fields), validated
# only for shape, not against VOCABULARY.
BEHAVIORAL_FIELDS = (
    "direction_support", "execution_capability", "entry_structure",
    "confirmation_structure", "exit_structure", "holding_horizon_class",
    "timeframe_class", "expected_frequency_class", "trend_dependency",
    "volatility_dependency", "volume_dependency", "session_dependency",
    "gap_dependency", "market_structure_dependency", "behavior_class",
    "required_regime", "invalidation_regime", "feature_dependencies",
)

IDENTITY_FIELDS = ("family_architecture", "strategy_version")
METADATA_FIELDS = ("evidence_confidence",)


def build_dna_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate + canonicalize a DNA payload. Raises ValueError on any field
    missing, unknown, or outside the controlled vocabulary."""

    expected = set(IDENTITY_FIELDS) | set(BEHAVIORAL_FIELDS) | set(METADATA_FIELDS)
    provided = set(raw)
    if provided != expected:
        missing = sorted(expected - provided)
        unknown = sorted(provided - expected)
        raise ValueError(f"DNA payload mismatch. missing={missing} unknown={unknown}")

    canonical: dict[str, Any] = {}
    for field in sorted(expected):
        value = raw[field]
        if field in LIST_FIELDS:
            if not isinstance(value, (list, tuple)) or not value:
                raise ValueError(f"DNA field {field} must be a non-empty list")
            items = sorted(str(item) for item in value)
            if field != "feature_dependencies":
                bad = [item for item in items if item not in VOCABULARY[field]]
                if bad:
                    raise ValueError(f"DNA field {field} has values outside the vocabulary: {bad}")
            canonical[field] = items
        else:
            value = str(value)
            if field in VOCABULARY and value not in VOCABULARY[field]:
                raise ValueError(f"DNA field {field}={value!r} is outside the vocabulary {VOCABULARY[field]}")
            canonical[field] = value
    return canonical


def compute_fingerprint(payload: dict[str, Any], *, dna_schema_version: int = DNA_SCHEMA_VERSION) -> str:
    canonical = build_dna_payload(payload)
    body = json.dumps({"dna_schema_version": dna_schema_version, **canonical}, sort_keys=True, separators=(",", ":"))
    return sha256(body.encode("utf-8")).hexdigest()


def behavioral_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Fraction of behavioral fields that agree (list fields via Jaccard).
    Identity fields are deliberately excluded so this measures behavior, not
    naming. 1.0 = behaviorally identical; 0.0 = disagrees everywhere."""

    a = build_dna_payload(a)
    b = build_dna_payload(b)
    total = 0.0
    for field in BEHAVIORAL_FIELDS:
        if field in LIST_FIELDS:
            set_a, set_b = set(a[field]), set(b[field])
            union = set_a | set_b
            total += (len(set_a & set_b) / len(union)) if union else 1.0
        else:
            total += 1.0 if a[field] == b[field] else 0.0
    return round(total / len(BEHAVIORAL_FIELDS), 4)


# ---------------------------------------------------------------------------
# DNA definitions. Every existing family is described exactly as it was
# built and evidenced -- backfilling DNA never changes historical results,
# it only labels them. evidence_confidence reflects the Phase 12 pilots'
# honest outcomes (docs/2026-07-23-* and 2026-07-24-phase12-4-*).
# ---------------------------------------------------------------------------

def _dna(architecture: str, version: str, **fields: Any) -> dict[str, Any]:
    return {"family_architecture": architecture, "strategy_version": version, **fields}


_INTRADAY_COMMON = {
    "holding_horizon_class": "intraday_hours",
    "timeframe_class": "intraday_15m_30m",
    "expected_frequency_class": "multiple_per_session",
    "execution_capability": "simulation_only",
    "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
}

FAMILY_DNA: dict[str, dict[str, Any]] = {
    "opening_range_breakout_v1": _dna(
        "opening_range_breakout_v1", "v1", **_INTRADAY_COMMON,
        direction_support=["long", "short"], entry_structure="range_breakout",
        confirmation_structure=["closing_confirmation"],
        trend_dependency="benefits_from_trend", volatility_dependency="agnostic",
        volume_dependency="agnostic", session_dependency="any_session_time",
        gap_dependency="agnostic", market_structure_dependency="agnostic",
        behavior_class="momentum", required_regime=["any"], invalidation_regime=["range_bound"],
        feature_dependencies=["opening_range_high", "opening_range_low", "minutes_from_open"],
        evidence_confidence="tested_negative_archived",
    ),
    "vwap_reversion_v1": _dna(
        "vwap_reversion_v1", "v1", **_INTRADAY_COMMON,
        direction_support=["long", "short"], entry_structure="vwap_deviation_fade",
        confirmation_structure=["none"],
        trend_dependency="requires_range", volatility_dependency="agnostic",
        volume_dependency="agnostic", session_dependency="any_session_time",
        gap_dependency="agnostic", market_structure_dependency="agnostic",
        behavior_class="mean_reversion", required_regime=["any"], invalidation_regime=["trending_up", "trending_down"],
        feature_dependencies=["session_vwap", "distance_from_session_vwap"],
        evidence_confidence="tested_negative_archived",
    ),
    "gap_fill_v1": _dna(
        "gap_fill_v1", "v1", **_INTRADAY_COMMON,
        direction_support=["long", "short"], entry_structure="gap_open_fade",
        confirmation_structure=["relative_volume"],
        trend_dependency="agnostic", volatility_dependency="agnostic",
        volume_dependency="requires_confirmation", session_dependency="first_two_hours",
        gap_dependency="requires_gap", market_structure_dependency="agnostic",
        behavior_class="mean_reversion", required_regime=["any"], invalidation_regime=["none_declared"],
        feature_dependencies=["gap_percent", "session_relative_volume", "minutes_from_open"],
        evidence_confidence="tested_negative_archived",
    ),
    "session_momentum_v1": _dna(
        "session_momentum_v1", "v1", **_INTRADAY_COMMON,
        direction_support=["long", "short"], entry_structure="momentum_bar_continuation",
        confirmation_structure=["relative_volume"],
        trend_dependency="benefits_from_trend", volatility_dependency="agnostic",
        volume_dependency="requires_confirmation", session_dependency="any_session_time",
        gap_dependency="agnostic", market_structure_dependency="agnostic",
        behavior_class="momentum", required_regime=["any"], invalidation_regime=["range_bound"],
        feature_dependencies=["session_relative_volume"],
        evidence_confidence="specialist_lead",  # AMD 30m long thread (research_specialist_threads)
    ),
    "intraday_trend_pullback_v1": _dna(
        "intraday_trend_pullback_v1", "v1", **_INTRADAY_COMMON,
        direction_support=["long", "short"], entry_structure="trend_pullback",
        confirmation_structure=["vwap_alignment"],
        trend_dependency="requires_trend", volatility_dependency="agnostic",
        volume_dependency="agnostic", session_dependency="any_session_time",
        gap_dependency="agnostic", market_structure_dependency="uses_structure_context",
        behavior_class="hybrid", required_regime=["trending_up", "trending_down"], invalidation_regime=["range_bound"],
        feature_dependencies=["distance_from_session_vwap"],
        evidence_confidence="tested_negative_archived",
    ),
    "ema_trend_continuation_v1": _dna(
        "ema_trend_continuation_v1", "v1", **_INTRADAY_COMMON,
        direction_support=["long", "short"], entry_structure="indicator_crossover",
        confirmation_structure=["momentum_direction"],
        trend_dependency="requires_trend", volatility_dependency="agnostic",
        volume_dependency="agnostic", session_dependency="any_session_time",
        gap_dependency="agnostic", market_structure_dependency="agnostic",
        behavior_class="momentum", required_regime=["trending_up", "trending_down"], invalidation_regime=["range_bound"],
        feature_dependencies=["ema_fast", "ema_slow"],
        evidence_confidence="tested_negative_archived",
    ),
    "opening_fade_v1": _dna(
        "opening_fade_v1", "v1", **_INTRADAY_COMMON,
        direction_support=["long", "short"], entry_structure="opening_range_extension_fade",
        confirmation_structure=["none"],
        trend_dependency="requires_range", volatility_dependency="agnostic",
        volume_dependency="agnostic", session_dependency="first_two_hours",
        gap_dependency="agnostic", market_structure_dependency="agnostic",
        behavior_class="mean_reversion", required_regime=["any"], invalidation_regime=["trending_up", "trending_down"],
        feature_dependencies=["opening_range_high", "opening_range_low"],
        evidence_confidence="tested_negative_archived",
    ),
    "vwap_trend_continuation_v1": _dna(
        "vwap_trend_continuation_v1", "v1", **_INTRADAY_COMMON,
        direction_support=["long", "short"], entry_structure="vwap_pullback",
        confirmation_structure=["momentum_direction"],
        trend_dependency="requires_trend", volatility_dependency="agnostic",
        volume_dependency="agnostic", session_dependency="any_session_time",
        gap_dependency="agnostic", market_structure_dependency="agnostic",
        behavior_class="momentum", required_regime=["trending_up", "trending_down"], invalidation_regime=["range_bound"],
        feature_dependencies=["session_vwap", "distance_from_session_vwap"],
        evidence_confidence="tested_negative_archived",
    ),
}


def register_family_dna(architecture: str, payload: dict[str, Any]) -> None:
    """Called by Strategy Engine V2 family modules at import time so each
    family owns its DNA next to its strategy logic. Validates immediately --
    a family with malformed DNA fails at import, not at backfill time."""

    build_dna_payload(payload)
    if architecture in FAMILY_DNA:
        existing = compute_fingerprint(FAMILY_DNA[architecture])
        incoming = compute_fingerprint(payload)
        if existing != incoming:
            raise ValueError(f"DNA for {architecture} already registered with a different fingerprint")
        return
    FAMILY_DNA[architecture] = payload


def backfill_strategy_dna(conn: psycopg.Connection) -> dict[str, Any]:
    """Idempotently write one strategy_dna row per registered family at the
    current DNA_SCHEMA_VERSION. Never updates an existing row (append-only
    table; ON CONFLICT DO NOTHING). Historical results are untouched -- this
    only labels families that already exist."""

    written: list[str] = []
    skipped: list[str] = []
    for architecture, payload in sorted(FAMILY_DNA.items()):
        canonical = build_dna_payload(payload)
        fingerprint = compute_fingerprint(payload)
        row = conn.execute(
            """
            INSERT INTO strategy_dna(family_architecture, strategy_version, dna_schema_version, fingerprint, dna)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (family_architecture, strategy_version, dna_schema_version) DO NOTHING
            RETURNING id
            """,
            (architecture, canonical["strategy_version"], DNA_SCHEMA_VERSION, fingerprint, Jsonb(canonical)),
        ).fetchone()
        (written if row else skipped).append(architecture)
    conn.commit()
    return {"dna_schema_version": DNA_SCHEMA_VERSION, "written": written, "already_present": skipped}


def list_strategy_dna(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (family_architecture, strategy_version)
               id, family_architecture, strategy_version, dna_schema_version, fingerprint, dna, superseded_by_id, created_at
        FROM strategy_dna
        ORDER BY family_architecture, strategy_version, dna_schema_version DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def get_strategy_dna(conn: psycopg.Connection, family_architecture: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, family_architecture, strategy_version, dna_schema_version, fingerprint, dna, superseded_by_id, created_at
        FROM strategy_dna
        WHERE family_architecture = %s
        ORDER BY dna_schema_version DESC
        LIMIT 1
        """,
        (family_architecture,),
    ).fetchone()
    return dict(row) if row else None


def dna_similarity_matrix(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Pairwise behavioral similarity across all latest DNA rows. Small n
    (tens of families), so O(n^2) is fine and keeps this deterministic."""

    rows = list_strategy_dna(conn)
    pairs = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            pairs.append(
                {
                    "a": a["family_architecture"],
                    "b": b["family_architecture"],
                    "behavioral_similarity": behavioral_similarity(a["dna"], b["dna"]),
                }
            )
    return sorted(pairs, key=lambda pair: -pair["behavioral_similarity"])
