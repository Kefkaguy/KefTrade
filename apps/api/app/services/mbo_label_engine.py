"""Stage 2A: causal forward-label construction.

Builds forward labels against the **frozen** Stage-1 v2 feature dataset. It
reads that dataset and never writes to it; labels land in their own files, keyed
so they can be joined back.

This module computes **no** correlation, no information coefficient, no ranking,
no threshold and no P/L. It produces the dependent variable and stops. Freezing
the label definitions before any relationship is inspected is the point: a
horizon chosen after seeing which one worked is not a horizon, it is a result.

## Horizons, frozen

Seven, declared together so a disappointing one cannot be dropped afterwards
and none can be substituted for another.

| Name | Kind | Definition |
|---|---|---|
| `next_change` | changes | the next snapshot whose midpoint differs from the source |
| `next_2_changes` | changes | the second such snapshot |
| `1s` `5s` `10s` `30s` `60s` | time | the first valid future coherent state at or after `source + H` |

**Change-count horizons are defined on the snapshot sequence of the same
cadence**, not on raw book events. A "next midpoint change" at the 1s cadence is
the next *second* whose midpoint differs, which is a coarser object than the
next event-time tick. That is a real limitation of labelling from the frozen
snapshot set rather than by re-replaying 562 M events, and it is stated here
rather than left for a reader to infer.

## The at-or-after rule

For a time horizon `H`, the target instant is `t_target = source_ts + H`. The
label is the **first** snapshot at or after `t_target` that carries a coherent
midpoint. Not the nearest; not the one before. If the first candidate at or
after `t_target` has no midpoint, the scan continues forward, and the number
skipped is recorded.

`realized_lag_ns = label_ts - t_target` is preserved, and is always `>= 0`. A
label whose realized lag is large is a thin-book observation, not a 60-second
horizon quietly relabelled -- and keeping the lag is what lets a later stage
tell the difference.

**No substitution.** If no valid state exists at or after `t_target` inside the
symbol-day, the label is missing with a named reason. It is never backfilled
from an earlier state, and never taken from the next session.

## Session edges

One Parquet file is one symbol-day, and a label search never leaves it. Nothing
is carried across the session boundary: an overnight gap is not a 60-second
horizon. A horizon extending past the last available coherent state yields
`session_end_before_horizon`.

## Missing labels are named, never imputed

Every row gets a `label_status`. There is no fill value, no forward fill and no
interpolation, because a filled label is an invented observation that would
count toward significance as though it had been measured.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.mbo_feature_engine import (
    FEATURE_VOCABULARY_HASH,
)

LABEL_ENGINE_VERSION = "tier1_mbo_label_engine_v1"

# The frozen Stage-1 artefact these labels are built against. Recorded so a
# label set can never be silently paired with a different feature semantics.
REQUIRED_FEATURE_ENGINE_VERSION = "tier1_mbo_feature_engine_v2"
REQUIRED_FEATURE_SEMANTICS_HASH = (
    "4aaeb9cb6d6700524d7fb065036612376d482a5cdff47d555d42c8a895c62551"
)

NANOS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class Horizon:
    """One frozen forward horizon."""

    name: str
    kind: str  # "time" or "changes"
    #: nanoseconds for "time"; a count of distinct midpoint changes otherwise
    magnitude: int


HORIZONS: tuple[Horizon, ...] = (
    Horizon("next_change", "changes", 1),
    Horizon("next_2_changes", "changes", 2),
    Horizon("1s", "time", 1 * NANOS_PER_SECOND),
    Horizon("5s", "time", 5 * NANOS_PER_SECOND),
    Horizon("10s", "time", 10 * NANOS_PER_SECOND),
    Horizon("30s", "time", 30 * NANOS_PER_SECOND),
    Horizon("60s", "time", 60 * NANOS_PER_SECOND),
)

HORIZON_NAMES: tuple[str, ...] = tuple(h.name for h in HORIZONS)

# ---------------------------------------------------------------------------
# Label status vocabulary. Fixed strings: a missing label must always be
# missing for a stated reason, and the reasons must be comparable across runs.
# ---------------------------------------------------------------------------

LABEL_OK = "ok"
LABEL_SOURCE_MIDPOINT_UNAVAILABLE = "source_midpoint_unavailable"
LABEL_SESSION_END_BEFORE_HORIZON = "session_end_before_horizon"
LABEL_NO_FURTHER_MIDPOINT_CHANGE = "no_further_midpoint_change"
LABEL_NO_VALID_FUTURE_STATE = "no_valid_future_state"

LABEL_STATUSES: tuple[str, ...] = (
    LABEL_OK,
    LABEL_SOURCE_MIDPOINT_UNAVAILABLE,
    LABEL_SESSION_END_BEFORE_HORIZON,
    LABEL_NO_FURTHER_MIDPOINT_CHANGE,
    LABEL_NO_VALID_FUTURE_STATE,
)

LABEL_COLUMNS: tuple[str, ...] = (
    # Join key back to the frozen feature row.
    "symbol",
    "session_date",
    "cadence",
    "sequence_index",
    "horizon",
    "horizon_kind",
    "horizon_magnitude",
    # Source side.
    "source_ts_event",
    "source_grid_ts_event",
    "source_midpoint",
    "source_ts_recv",
    "source_feature_available_ts_recv",
    # Target and realized label side.
    "target_ts_event",
    "label_sequence_index",
    "label_ts_event",
    "label_ts_recv",
    "realized_lag_ns",
    "skipped_incoherent_states",
    "future_midpoint",
    "midpoint_change",
    "return_bps",
    # Provenance for latency simulation: when the label could first be known.
    "label_available_ts_recv",
    "label_status",
)

LABEL_SCHEMA_HASH = hashlib.sha256("\n".join(LABEL_COLUMNS).encode("utf-8")).hexdigest()

LABEL_DEFINITION_HASH = hashlib.sha256(
    "\n".join(
        (
            LABEL_ENGINE_VERSION,
            REQUIRED_FEATURE_SEMANTICS_HASH,
            *(f"{h.name}:{h.kind}:{h.magnitude}" for h in HORIZONS),
            *LABEL_COLUMNS,
        )
    ).encode("utf-8")
).hexdigest()


# ---------------------------------------------------------------------------
# The snapshot spine a label set is built from
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SnapshotSpine:
    """The minimal per-snapshot columns labelling needs, in emission order.

    Deliberately narrow: labelling reads five columns out of 73, so a symbol-day
    spine is small even where the feature file is not.
    """

    symbol: str
    session_date: str
    cadence: str
    sequence_index: list[int]
    ts_event: list[int]
    grid_ts_event: list[int | None]
    ts_recv: list[int]
    feature_available_ts_recv: list[int]
    midpoint: list[float | None]

    def __len__(self) -> int:
        return len(self.sequence_index)


SPINE_COLUMNS: tuple[str, ...] = (
    "symbol",
    "session_date",
    "cadence",
    "sequence_index",
    "ts_event",
    "grid_ts_event",
    "source_ts_recv",
    "feature_available_ts_recv",
    "midpoint",
)


def spine_from_columns(
    *,
    symbol: str,
    session_date: str,
    cadence: str,
    columns: dict[str, list[Any]],
) -> SnapshotSpine:
    """Build a spine from already-extracted columns, in file order."""
    return SnapshotSpine(
        symbol=symbol,
        session_date=session_date,
        cadence=cadence,
        sequence_index=[int(v) for v in columns["sequence_index"]],
        ts_event=[int(v) for v in columns["ts_event"]],
        grid_ts_event=[None if v is None else int(v) for v in columns["grid_ts_event"]],
        ts_recv=[int(v or 0) for v in columns["source_ts_recv"]],
        feature_available_ts_recv=[
            int(v or 0) for v in columns["feature_available_ts_recv"]
        ],
        midpoint=[None if v is None else float(v) for v in columns["midpoint"]],
    )


def read_spine(path: str) -> SnapshotSpine:
    """Read the labelling spine out of one frozen Stage-1 Parquet file.

    Reads a projection, never the whole feature table, and never writes.
    """
    import pyarrow.parquet as pq

    table = pq.read_table(path, columns=list(SPINE_COLUMNS))
    data = table.to_pydict()
    symbols = {s for s in data["symbol"] if s}
    dates = {d for d in data["session_date"] if d}
    cadences = {c for c in data["cadence"] if c}
    if len(symbols) != 1 or len(dates) != 1 or len(cadences) != 1:
        raise ValueError(
            f"{path} mixes symbol/session/cadence ({symbols}, {dates}, {cadences}); "
            "labels are constructed per symbol-day per cadence"
        )
    return spine_from_columns(
        symbol=symbols.pop(),
        session_date=dates.pop(),
        cadence=cadences.pop(),
        columns=data,
    )


# ---------------------------------------------------------------------------
# Label construction
# ---------------------------------------------------------------------------


def _coherent_indices(spine: SnapshotSpine) -> list[int]:
    """Positions carrying a usable midpoint, in order.

    A snapshot with no midpoint is not a coherent book state: one side of the
    touch was empty. It cannot be a label, and it cannot be a source.
    """
    return [i for i, mid in enumerate(spine.midpoint) if mid is not None]


def _next_change_map(spine: SnapshotSpine, coherent: Sequence[int]) -> dict[int, int]:
    """For each coherent position, the next coherent position whose midpoint differs.

    Built in one backward pass, so a session where the midpoint sits still for
    thousands of snapshots costs the same as one where it never does.
    """
    following: dict[int, int] = {}
    successor: int | None = None
    for position in reversed(range(len(coherent))):
        index = coherent[position]
        if position + 1 < len(coherent):
            candidate = coherent[position + 1]
            if spine.midpoint[candidate] != spine.midpoint[index]:
                successor = candidate
            else:
                successor = following.get(candidate)
        else:
            successor = None
        if successor is not None:
            following[index] = successor
    return following


def _empty_row(
    spine: SnapshotSpine,
    index: int,
    horizon: Horizon,
    status: str,
    *,
    target_ts: int | None,
) -> dict[str, Any]:
    return {
        "symbol": spine.symbol,
        "session_date": spine.session_date,
        "cadence": spine.cadence,
        "sequence_index": spine.sequence_index[index],
        "horizon": horizon.name,
        "horizon_kind": horizon.kind,
        "horizon_magnitude": horizon.magnitude,
        "source_ts_event": spine.ts_event[index],
        "source_grid_ts_event": spine.grid_ts_event[index],
        "source_midpoint": spine.midpoint[index],
        "source_ts_recv": spine.ts_recv[index],
        "source_feature_available_ts_recv": spine.feature_available_ts_recv[index],
        "target_ts_event": target_ts,
        "label_sequence_index": None,
        "label_ts_event": None,
        "label_ts_recv": None,
        "realized_lag_ns": None,
        "skipped_incoherent_states": None,
        "future_midpoint": None,
        "midpoint_change": None,
        "return_bps": None,
        "label_available_ts_recv": None,
        "label_status": status,
    }


def build_labels(spine: SnapshotSpine, horizons: Sequence[Horizon] = HORIZONS) -> Iterator[dict[str, Any]]:
    """Yield one label row per (snapshot, horizon). Never imputes."""
    coherent = _coherent_indices(spine)
    coherent_set = set(coherent)
    change_map = _next_change_map(spine, coherent)
    total = len(spine)

    for index in range(total):
        source_mid = spine.midpoint[index]
        source_ts = spine.ts_event[index]
        for horizon in horizons:
            target_ts = (
                source_ts + horizon.magnitude if horizon.kind == "time" else None
            )
            if source_mid is None or index not in coherent_set:
                yield _empty_row(
                    spine,
                    index,
                    horizon,
                    LABEL_SOURCE_MIDPOINT_UNAVAILABLE,
                    target_ts=target_ts,
                )
                continue

            if horizon.kind == "changes":
                label_index: int | None = index
                for _ in range(horizon.magnitude):
                    label_index = change_map.get(label_index) if label_index is not None else None
                    if label_index is None:
                        break
                if label_index is None:
                    yield _empty_row(
                        spine,
                        index,
                        horizon,
                        LABEL_NO_FURTHER_MIDPOINT_CHANGE,
                        target_ts=None,
                    )
                    continue
                skipped = 0
                realized_lag = None
            else:
                assert target_ts is not None
                label_index = None
                skipped = 0
                # First coherent state at or after the target. Not the nearest,
                # and never one before it.
                for candidate in range(index + 1, total):
                    if spine.ts_event[candidate] < target_ts:
                        continue
                    if spine.midpoint[candidate] is None:
                        skipped += 1
                        continue
                    label_index = candidate
                    break
                if label_index is None:
                    reached_target = any(
                        spine.ts_event[c] >= target_ts for c in range(index + 1, total)
                    )
                    yield _empty_row(
                        spine,
                        index,
                        horizon,
                        LABEL_NO_VALID_FUTURE_STATE
                        if reached_target
                        else LABEL_SESSION_END_BEFORE_HORIZON,
                        target_ts=target_ts,
                    )
                    continue
                realized_lag = spine.ts_event[label_index] - target_ts

            future_mid = spine.midpoint[label_index]
            assert future_mid is not None
            change = future_mid - source_mid
            row = _empty_row(spine, index, horizon, LABEL_OK, target_ts=target_ts)
            row.update(
                {
                    "label_sequence_index": spine.sequence_index[label_index],
                    "label_ts_event": spine.ts_event[label_index],
                    "label_ts_recv": spine.ts_recv[label_index],
                    "realized_lag_ns": realized_lag,
                    "skipped_incoherent_states": skipped,
                    "future_midpoint": future_mid,
                    "midpoint_change": change,
                    "return_bps": (change / source_mid * 10_000) if source_mid else None,
                    # When the label could first have been known: the later of
                    # the two records it rests on. Never earlier than the
                    # feature row it will be joined to.
                    "label_available_ts_recv": max(
                        spine.feature_available_ts_recv[index],
                        spine.feature_available_ts_recv[label_index],
                        spine.ts_recv[label_index],
                    ),
                }
            )
            yield row


def label_definitions() -> dict[str, Any]:
    """The frozen label specification, as data."""
    return {
        "label_engine_version": LABEL_ENGINE_VERSION,
        "label_definition_hash": LABEL_DEFINITION_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "built_against": {
            "feature_engine_version": REQUIRED_FEATURE_ENGINE_VERSION,
            "feature_semantics_hash": REQUIRED_FEATURE_SEMANTICS_HASH,
            "feature_vocabulary_hash": FEATURE_VOCABULARY_HASH,
            "features_modified": False,
        },
        "horizons": [
            {"name": h.name, "kind": h.kind, "magnitude": h.magnitude} for h in HORIZONS
        ],
        "columns": list(LABEL_COLUMNS),
        "statuses": list(LABEL_STATUSES),
        "rules": {
            "time_horizon": (
                "label = first snapshot at or after source_ts + H carrying a coherent "
                "midpoint. Not the nearest; never one before the target. "
                "realized_lag_ns = label_ts - target_ts, always >= 0."
            ),
            "change_horizon": (
                "label = the Nth following coherent snapshot whose midpoint differs "
                "from its predecessor, defined on the snapshot sequence of the same "
                "cadence -- coarser than an event-time tick."
            ),
            "session_edges": (
                "One file is one symbol-day; a label search never leaves it. Nothing "
                "is carried across a session boundary."
            ),
            "missing_labels": (
                "Named via label_status. Never imputed, forward-filled or "
                "interpolated: a filled label would count toward significance as "
                "though it had been measured."
            ),
            "no_substitution": (
                "Horizons are frozen together. A horizon is never replaced by a "
                "nearer one, and none may be added or dropped after outcomes."
            ),
        },
        "contains_predictive_result": False,
    }


def label_status_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Coverage by horizon and status. Counts only -- no relationship to features."""
    by_horizon: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_horizon.setdefault(
            row["horizon"], dict.fromkeys(LABEL_STATUSES, 0)
        )
        bucket[row["label_status"]] += 1
    return {
        "label_engine_version": LABEL_ENGINE_VERSION,
        "rows": len(rows),
        "by_horizon": {
            name: dict(by_horizon.get(name, dict.fromkeys(LABEL_STATUSES, 0)))
            for name in HORIZON_NAMES
        },
        "labelled_share_by_horizon": {
            name: (
                round(
                    by_horizon.get(name, {}).get(LABEL_OK, 0)
                    / sum(by_horizon.get(name, {}).values()),
                    6,
                )
                if sum(by_horizon.get(name, {}).values())
                else None
            )
            for name in HORIZON_NAMES
        },
        "contains_predictive_result": False,
    }
