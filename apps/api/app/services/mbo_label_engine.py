"""Stage 2A v2: exact event-time forward labels.

Labels are resolved from the **original certified XNAS MBO stream**, replayed
through the same ``MboBook`` the Tier-1 gate certified 160/160 on. v1 derived
them from the sampled Stage-1 cadence sequence, which made "the next midpoint
change" mean "the next *second* whose midpoint differs" -- a coarser object than
the event-time tick the hypothesis is about. That is corrected here.

Computes no correlation, no information coefficient, no ranking, no threshold
and no P/L. It produces the dependent variable and stops.

## Exact definitions

For a source snapshot at ``t_s`` with the midpoint its feature row carried:

| Horizon | Definition |
|---|---|
| `next_change` | the first completed `F_LAST` state with `ts_event > t_s` whose midpoint differs from the source midpoint |
| `next_2_changes` | the next completed `F_LAST` state after that whose midpoint differs from *its* midpoint -- the second event-time change |
| `1s` `5s` `10s` `30s` `60s` | the first coherent completed `F_LAST` state with `ts_event >= t_s + H` |

**A state before the target is never used.** For time horizons the label instant
is always `>= t_s + H`; for change horizons it is always `> t_s`.

Preserved per horizon: the exact target, the actual label event timestamp, its
``ts_recv``, the availability timestamp, and the realized lag. A large realized
lag is a thin-book observation, not a horizon quietly shortened, and keeping the
lag is what lets a later stage tell them apart.

## Streaming resolution

Each raw symbol-day is replayed **once**. 337 M book states are never
materialized: the resolver holds only the source snapshots still awaiting a
label, and every horizon resolves through a structure that costs amortized
constant time per resolution.

* Time horizons keep a FIFO per horizon. Targets are monotone in `t_s`, so the
  queue head is always the next to resolve.
* Change horizons group pending sources by the midpoint they are waiting to
  differ from. When a state arrives at midpoint `m`, every group whose key is
  not `m` resolves at once; the group keyed `m` waits. `next_2_changes` sources
  then re-enter the same structure keyed by the midpoint that just resolved
  their first change.

## Session edges

One raw file is one symbol-day and resolution never leaves it. An overnight gap
is not a 60-second horizon.

## Missing labels are named, never imputed

There is no fill value, no forward fill and no interpolation: a filled label
would count toward significance as though it had been measured.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.mbo_book_validator import F_LAST, MboBook, MboEvent

LABEL_ENGINE_VERSION = "tier1_mbo_label_engine_v2"

SUPERSEDED_LABEL_VERSIONS: tuple[dict[str, str], ...] = (
    {
        "version": "tier1_mbo_label_engine_v1",
        "commit": "f3289c9701ea8c7d431d941a60b20b5cf447c548",
        "superseded_before_outcome": "true",
        "reason": (
            "labels were derived from the sampled Stage-1 cadence sequence rather "
            "than the certified event stream, so change horizons measured the next "
            "sampled interval whose midpoint differed instead of the next event-time "
            "midpoint change; and labels were stored long (one row per source-horizon) "
            "rather than wide."
        ),
        "label_definition_hash": (
            "35249ad2d70ae4669e28"  # truncated in the v1 report; full value in git
        ),
    },
)

REQUIRED_FEATURE_ENGINE_VERSION = "tier1_mbo_feature_engine_v4"
REQUIRED_FEATURE_SEMANTICS_HASH = (
    "fbe8add54376592e4c1a7196124086f6c5a69bf3bd0748dc1f08fa7db0d7563c"
)

# The feature semantics this label family was originally declared over. The
# label logic did not change with the v3 absorption correction -- labels are
# resolved from the raw stream against the snapshot spine, and no feature value
# enters a label -- so labels already on disk remain valid, but only if the
# regenerated spine is proved identical. See LABEL_LOGIC_HASH.
SUPERSEDED_REQUIRED_FEATURE_SEMANTICS_HASHES: tuple[str, ...] = (
    "4aaeb9cb6d6700524d7fb065036612376d482a5cdff47d555d42c8a895c62551",
    "7f613b06e8ba25bc45947c1ea6d3558e4508f73e37d6ef09736ba91d2d3933eb",
)

NANOS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class Horizon:
    name: str
    kind: str  # "time" or "changes"
    magnitude: int  # nanoseconds for time; change count otherwise
    prefix: str  # column prefix; never starts with a digit


HORIZONS: tuple[Horizon, ...] = (
    Horizon("next_change", "changes", 1, "next_change"),
    Horizon("next_2_changes", "changes", 2, "next_2_changes"),
    Horizon("1s", "time", 1 * NANOS_PER_SECOND, "h1s"),
    Horizon("5s", "time", 5 * NANOS_PER_SECOND, "h5s"),
    Horizon("10s", "time", 10 * NANOS_PER_SECOND, "h10s"),
    Horizon("30s", "time", 30 * NANOS_PER_SECOND, "h30s"),
    Horizon("60s", "time", 60 * NANOS_PER_SECOND, "h60s"),
)

HORIZON_NAMES: tuple[str, ...] = tuple(h.name for h in HORIZONS)
HORIZONS_BY_NAME: dict[str, Horizon] = {h.name: h for h in HORIZONS}
TIME_HORIZONS: tuple[Horizon, ...] = tuple(h for h in HORIZONS if h.kind == "time")
CHANGE_HORIZONS: tuple[Horizon, ...] = tuple(h for h in HORIZONS if h.kind == "changes")

LABEL_OK = "ok"
LABEL_SOURCE_MIDPOINT_UNAVAILABLE = "source_midpoint_unavailable"
LABEL_SESSION_END_BEFORE_HORIZON = "session_end_before_horizon"
LABEL_NO_FURTHER_MIDPOINT_CHANGE = "no_further_midpoint_change"

LABEL_STATUSES: tuple[str, ...] = (
    LABEL_OK,
    LABEL_SOURCE_MIDPOINT_UNAVAILABLE,
    LABEL_SESSION_END_BEFORE_HORIZON,
    LABEL_NO_FURTHER_MIDPOINT_CHANGE,
)

# ---------------------------------------------------------------------------
# Wide schema: one row per Stage-1 source snapshot.
#
# v1 exploded each snapshot into seven rows, which multiplied 19.5 M snapshots
# into 137 M label rows -- larger than the feature set it describes. Wide keeps
# the join one-to-one.
# ---------------------------------------------------------------------------

SHARED_COLUMNS: tuple[str, ...] = (
    "symbol",
    "session_date",
    "cadence",
    "sequence_index",
    "source_ts_event",
    "source_grid_ts_event",
    "source_midpoint",
    "source_ts_recv",
    "source_feature_available_ts_recv",
)

PER_HORIZON_SUFFIXES: tuple[str, ...] = (
    "status",
    "target_ts_event",
    "label_ts_event",
    "label_ts_recv",
    "realized_lag_ns",
    "future_midpoint",
    "midpoint_change",
    "return_bps",
    "available_ts_recv",
)


def horizon_columns(horizon: Horizon) -> tuple[str, ...]:
    return tuple(f"{horizon.prefix}_{suffix}" for suffix in PER_HORIZON_SUFFIXES)


LABEL_COLUMNS: tuple[str, ...] = (
    *SHARED_COLUMNS,
    *(column for horizon in HORIZONS for column in horizon_columns(horizon)),
)

LABEL_SCHEMA_HASH = hashlib.sha256("\n".join(LABEL_COLUMNS).encode("utf-8")).hexdigest()

# What the labels ARE, independent of the feature-engine semantics they were
# declared over. Nothing here moves when a feature VALUE changes, because no
# feature value enters a label: labels are resolved from the raw certified
# stream against the snapshot spine. An unchanged value is therefore evidence
# that label files already on disk do not need re-replaying.
LABEL_LOGIC_HASH = hashlib.sha256(
    "\n".join(
        (
            LABEL_ENGINE_VERSION,
            "event_time_from_certified_mbo_stream",
            *(f"{h.name}:{h.kind}:{h.magnitude}" for h in HORIZONS),
            *LABEL_COLUMNS,
        )
    ).encode("utf-8")
).hexdigest()

# The full provenance binding: the logic PLUS the feature semantics it was
# declared against. This one does move with a Stage-1 semantic correction, and
# it is supposed to.
LABEL_DEFINITION_HASH = hashlib.sha256(
    "\n".join(
        (
            LABEL_ENGINE_VERSION,
            REQUIRED_FEATURE_SEMANTICS_HASH,
            "event_time_from_certified_mbo_stream",
            *(f"{h.name}:{h.kind}:{h.magnitude}" for h in HORIZONS),
            *LABEL_COLUMNS,
        )
    ).encode("utf-8")
).hexdigest()

SUPERSEDED_LABEL_DEFINITION_HASHES: tuple[dict[str, str], ...] = (
    {
        "label_definition_hash": (
            "75239cc325d7aaa12caf2a24dd4c6f378788fb2e360ff76281731204410e9d73"
        ),
        "superseded_before_outcome": "true",
        "declared_over_feature_semantics": (
            "7f613b06e8ba25bc45947c1ea6d3558e4508f73e37d6ef09736ba91d2d3933eb"
        ),
        "label_content_changed": "false",
        "reason": (
            "rebound from feature-engine v3 to v4 for the queue_persistence "
            "coherent-state correction. As with the v2 -> v3 rebinding, no "
            "feature value enters a label and the snapshot spine is untouched."
        ),
    },
    {
        "label_definition_hash": (
            "2e8ada7e56d780639a8427b4e88d5e464cb541feacaf0fc8dccf9519097677ac"
        ),
        "superseded_before_outcome": "true",
        "declared_over_feature_semantics": (
            "4aaeb9cb6d6700524d7fb065036612376d482a5cdff47d555d42c8a895c62551"
        ),
        "label_content_changed": "false",
        "reason": (
            "rebound from feature-engine v2 to v3. The absorption correction "
            "changed feature values, not the snapshot spine and not the label "
            "resolution, so label content is unaffected. Reuse is admissible "
            "only against a spine proved identical row for row."
        ),
    },
)


# ---------------------------------------------------------------------------
# Source snapshots
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceSnapshot:
    """One frozen Stage-1 feature row awaiting labels."""

    cadence: str
    sequence_index: int
    ts_event: int
    grid_ts_event: int | None
    midpoint: float | None
    ts_recv: int
    feature_available_ts_recv: int


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


def read_source_snapshots(paths: Sequence[str]) -> tuple[str, str, list[SourceSnapshot]]:
    """Read the Stage-1 rows for one symbol-day across every cadence file.

    Reads a nine-column projection out of 73 and never writes. Returns snapshots
    sorted by ``ts_event``, which is what the streaming resolver requires.
    """
    import pyarrow.parquet as pq

    symbols: set[str] = set()
    dates: set[str] = set()
    snapshots: list[SourceSnapshot] = []
    for path in paths:
        data = pq.read_table(path, columns=list(SPINE_COLUMNS)).to_pydict()
        symbols.update(s for s in data["symbol"] if s)
        dates.update(d for d in data["session_date"] if d)
        for index in range(len(data["sequence_index"])):
            snapshots.append(
                SourceSnapshot(
                    cadence=data["cadence"][index],
                    sequence_index=int(data["sequence_index"][index]),
                    ts_event=int(data["ts_event"][index]),
                    grid_ts_event=(
                        None
                        if data["grid_ts_event"][index] is None
                        else int(data["grid_ts_event"][index])
                    ),
                    midpoint=(
                        None
                        if data["midpoint"][index] is None
                        else float(data["midpoint"][index])
                    ),
                    ts_recv=int(data["source_ts_recv"][index] or 0),
                    feature_available_ts_recv=int(
                        data["feature_available_ts_recv"][index] or 0
                    ),
                )
            )
    if len(symbols) != 1 or len(dates) != 1:
        raise ValueError(
            f"source files mix symbol/session ({symbols}, {dates}); labels are "
            "resolved per symbol-day"
        )
    # Stable within a timestamp so the output order is deterministic.
    snapshots.sort(key=lambda s: (s.ts_event, s.cadence, s.sequence_index))
    return symbols.pop(), dates.pop(), snapshots


# ---------------------------------------------------------------------------
# Streaming resolution against the certified event stream
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Pending:
    """One source's in-flight label state."""

    snapshot: SourceSnapshot
    row: dict[str, Any]
    #: horizon name -> the midpoint this source is currently waiting to differ from
    awaiting_change: dict[str, float] = field(default_factory=dict)
    #: how many further changes each change-horizon still needs
    changes_remaining: dict[str, int] = field(default_factory=dict)
    unresolved: int = 0


def _blank_row(symbol: str, session_date: str, snapshot: SourceSnapshot) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "session_date": session_date,
        "cadence": snapshot.cadence,
        "sequence_index": snapshot.sequence_index,
        "source_ts_event": snapshot.ts_event,
        "source_grid_ts_event": snapshot.grid_ts_event,
        "source_midpoint": snapshot.midpoint,
        "source_ts_recv": snapshot.ts_recv,
        "source_feature_available_ts_recv": snapshot.feature_available_ts_recv,
    }
    for horizon in HORIZONS:
        for column in horizon_columns(horizon):
            row[column] = None
        row[f"{horizon.prefix}_status"] = LABEL_SESSION_END_BEFORE_HORIZON
        if horizon.kind == "time":
            row[f"{horizon.prefix}_target_ts_event"] = (
                snapshot.ts_event + horizon.magnitude
            )
        else:
            row[f"{horizon.prefix}_status"] = LABEL_NO_FURTHER_MIDPOINT_CHANGE
    return row


def _record(
    pending: _Pending,
    horizon: Horizon,
    *,
    ts_event: int,
    ts_recv: int,
    midpoint: float,
) -> None:
    snapshot = pending.snapshot
    source_mid = snapshot.midpoint
    assert source_mid is not None
    prefix = horizon.prefix
    row = pending.row
    change = midpoint - source_mid
    target = row.get(f"{prefix}_target_ts_event")
    row[f"{prefix}_status"] = LABEL_OK
    row[f"{prefix}_label_ts_event"] = ts_event
    row[f"{prefix}_label_ts_recv"] = ts_recv
    row[f"{prefix}_realized_lag_ns"] = None if target is None else ts_event - target
    row[f"{prefix}_future_midpoint"] = midpoint
    row[f"{prefix}_midpoint_change"] = change
    row[f"{prefix}_return_bps"] = (change / source_mid * 10_000) if source_mid else None
    # When the label could first have been known: never before the feature row
    # it joins to, nor before the record that resolved it.
    row[f"{prefix}_available_ts_recv"] = max(
        snapshot.feature_available_ts_recv, ts_recv
    )
    pending.unresolved -= 1


class EventTimeLabelResolver:
    """Resolves labels for a symbol-day in one pass over the event stream.

    Holds only the sources still awaiting a label. Nothing about the book is
    retained beyond the current state, so the 337 M certified book states are
    streamed rather than materialized.
    """

    def __init__(
        self,
        *,
        symbol: str,
        session_date: str,
        sources: Sequence[SourceSnapshot],
        horizons: Sequence[Horizon] = HORIZONS,
    ) -> None:
        self.symbol = symbol
        self.session_date = session_date
        self.horizons = tuple(horizons)
        self._time_horizons = tuple(h for h in self.horizons if h.kind == "time")
        self._change_horizons = tuple(h for h in self.horizons if h.kind == "changes")
        self._sources = list(sources)
        self._cursor = 0
        self._rows: dict[tuple[str, int], dict[str, Any]] = {}
        self._order: list[tuple[str, int]] = []
        self._pending: dict[tuple[str, int], _Pending] = {}
        # horizon name -> FIFO of keys, ordered by target (monotone in ts_event)
        self._time_queues: dict[str, list[tuple[str, int]]] = {
            h.name: [] for h in self._time_horizons
        }
        self._time_heads: dict[str, int] = {h.name: 0 for h in self._time_horizons}
        # horizon name -> midpoint being waited on -> keys
        self._change_groups: dict[str, dict[float, list[tuple[str, int]]]] = {
            h.name: {} for h in self._change_horizons
        }
        self.book = MboBook(max_recorded_violations=0)
        self.flast_states = 0
        self.records = 0

    # -- source activation -------------------------------------------------

    def _activate(self, ts_event: int) -> None:
        """Admit every source strictly before ``ts_event`` as pending."""
        while self._cursor < len(self._sources):
            snapshot = self._sources[self._cursor]
            if snapshot.ts_event >= ts_event:
                break
            self._cursor += 1
            key = (snapshot.cadence, snapshot.sequence_index)
            row = _blank_row(self.symbol, self.session_date, snapshot)
            self._rows[key] = row
            self._order.append(key)
            if snapshot.midpoint is None:
                # No coherent source midpoint: no horizon is resolvable, and the
                # reason is the source, not the future.
                for horizon in self.horizons:
                    row[f"{horizon.prefix}_status"] = LABEL_SOURCE_MIDPOINT_UNAVAILABLE
                continue
            pending = _Pending(snapshot=snapshot, row=row)
            for horizon in self._time_horizons:
                self._time_queues[horizon.name].append(key)
                pending.unresolved += 1
            for horizon in self._change_horizons:
                pending.awaiting_change[horizon.name] = snapshot.midpoint
                pending.changes_remaining[horizon.name] = horizon.magnitude
                self._change_groups[horizon.name].setdefault(
                    snapshot.midpoint, []
                ).append(key)
                pending.unresolved += 1
            self._pending[key] = pending

    # -- resolution --------------------------------------------------------

    def _resolve_time(self, ts_event: int, ts_recv: int, midpoint: float) -> None:
        for horizon in self._time_horizons:
            queue = self._time_queues[horizon.name]
            head = self._time_heads[horizon.name]
            while head < len(queue):
                key = queue[head]
                pending = self._pending.get(key)
                if pending is None:  # already fully resolved and released
                    head += 1
                    continue
                target = pending.row[f"{horizon.prefix}_target_ts_event"]
                if target > ts_event:
                    break
                _record(
                    pending,
                    horizon,
                    ts_event=ts_event,
                    ts_recv=ts_recv,
                    midpoint=midpoint,
                )
                head += 1
                self._release(key, pending)
            self._time_heads[horizon.name] = head

    def _resolve_changes(self, ts_event: int, ts_recv: int, midpoint: float) -> None:
        for horizon in self._change_horizons:
            groups = self._change_groups[horizon.name]
            # Every group whose awaited midpoint is not the current one changed.
            for awaited in [key for key in groups if key != midpoint]:
                keys = groups.pop(awaited)
                for key in keys:
                    pending = self._pending.get(key)
                    if pending is None:
                        continue
                    remaining = pending.changes_remaining[horizon.name] - 1
                    pending.changes_remaining[horizon.name] = remaining
                    if remaining == 0:
                        _record(
                            pending,
                            horizon,
                            ts_event=ts_event,
                            ts_recv=ts_recv,
                            midpoint=midpoint,
                        )
                        self._release(key, pending)
                    else:
                        # The next change must differ from the midpoint that
                        # just resolved this one.
                        pending.awaiting_change[horizon.name] = midpoint
                        groups.setdefault(midpoint, []).append(key)

    def _release(self, key: tuple[str, int], pending: _Pending) -> None:
        if pending.unresolved <= 0:
            self._pending.pop(key, None)

    # -- driver ------------------------------------------------------------

    def process(self, events: Iterable[MboEvent]) -> None:
        """Replay the certified stream once, resolving pending labels as it goes."""
        for event in events:
            self.records += 1
            self.book.apply(event)
            if not (event.flags & F_LAST):
                continue
            self.flast_states += 1
            bid = self.book.best_bid()
            ask = self.book.best_ask()
            ts_event = event.ts_event
            # Sources strictly before this state become eligible: "subsequent"
            # means ts_event > t_s.
            self._activate(ts_event)
            if bid is None or ask is None:
                # Not a coherent state. It cannot be a label, and a change
                # horizon must not treat a one-sided book as a midpoint change.
                continue
            midpoint = (bid.price + ask.price) / 2
            self._resolve_time(ts_event, event.ts_recv, midpoint)
            self._resolve_changes(ts_event, event.ts_recv, midpoint)

        # Anything still pending never got its state inside the symbol-day.
        # Blank rows already carry the correct terminal status, and sources
        # never activated are emitted with it too.
        self._activate_remaining()

    def _activate_remaining(self) -> None:
        while self._cursor < len(self._sources):
            snapshot = self._sources[self._cursor]
            self._cursor += 1
            key = (snapshot.cadence, snapshot.sequence_index)
            row = _blank_row(self.symbol, self.session_date, snapshot)
            if snapshot.midpoint is None:
                for horizon in self.horizons:
                    row[f"{horizon.prefix}_status"] = LABEL_SOURCE_MIDPOINT_UNAVAILABLE
            self._rows[key] = row
            self._order.append(key)

    def rows(self) -> Iterator[dict[str, Any]]:
        """One row per source snapshot, in source order."""
        for key in self._order:
            yield self._rows[key]


def resolve_symbol_day_labels(
    *,
    symbol: str,
    session_date: str,
    sources: Sequence[SourceSnapshot],
    events: Iterable[MboEvent],
    horizons: Sequence[Horizon] = HORIZONS,
) -> list[dict[str, Any]]:
    """Convenience wrapper: one replay, one wide row per source snapshot."""
    resolver = EventTimeLabelResolver(
        symbol=symbol, session_date=session_date, sources=sources, horizons=horizons
    )
    resolver.process(events)
    return list(resolver.rows())


def label_definitions() -> dict[str, Any]:
    return {
        "label_engine_version": LABEL_ENGINE_VERSION,
        "label_definition_hash": LABEL_DEFINITION_HASH,
        "label_schema_hash": LABEL_SCHEMA_HASH,
        "superseded_label_versions": [dict(e) for e in SUPERSEDED_LABEL_VERSIONS],
        "label_source": "certified XNAS MBO stream replayed through MboBook",
        "derived_from_sampled_cadence": False,
        "storage": "wide -- one row per Stage-1 source snapshot",
        "row_multiplier_vs_sources": 1,
        "built_against": {
            "feature_engine_version": REQUIRED_FEATURE_ENGINE_VERSION,
            "feature_semantics_hash": REQUIRED_FEATURE_SEMANTICS_HASH,
            "features_modified": False,
        },
        "horizons": [
            {
                "name": h.name,
                "kind": h.kind,
                "magnitude": h.magnitude,
                "column_prefix": h.prefix,
            }
            for h in HORIZONS
        ],
        "columns": list(LABEL_COLUMNS),
        "per_horizon_suffixes": list(PER_HORIZON_SUFFIXES),
        "statuses": list(LABEL_STATUSES),
        "rules": {
            "time_horizon": (
                "first coherent completed F_LAST state with ts_event >= t_s + H. A "
                "state before the target is never used. realized_lag_ns = "
                "label_ts_event - target, always >= 0."
            ),
            "change_horizon": (
                "next_change is the first completed F_LAST state with ts_event > t_s "
                "whose midpoint differs from the source midpoint. next_2_changes is "
                "the next state after that whose midpoint differs from the first "
                "change's midpoint. Event-time, not cadence-sampled."
            ),
            "incoherent_states": (
                "A one-sided book is not a coherent state: it cannot be a label, and "
                "a change horizon must not read it as a midpoint change."
            ),
            "session_edges": (
                "One raw file is one symbol-day; resolution never leaves it."
            ),
            "missing_labels": "named via per-horizon status; never imputed",
            "no_substitution": (
                "Horizons are frozen together; none is replaced by a nearer one."
            ),
        },
        "contains_predictive_result": False,
    }


def label_status_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Per-horizon status counts. Counts only -- no relationship to features."""
    by_horizon: dict[str, dict[str, int]] = {
        h.name: dict.fromkeys(LABEL_STATUSES, 0) for h in HORIZONS
    }
    by_cadence: dict[str, int] = {}
    for row in rows:
        by_cadence[row["cadence"]] = by_cadence.get(row["cadence"], 0) + 1
        for horizon in HORIZONS:
            status = row[f"{horizon.prefix}_status"]
            by_horizon[horizon.name][status] += 1
    return {
        "label_engine_version": LABEL_ENGINE_VERSION,
        "rows": len(rows),
        "rows_by_cadence": dict(sorted(by_cadence.items())),
        "by_horizon": by_horizon,
        "labelled_share_by_horizon": {
            name: (round(counts[LABEL_OK] / len(rows), 6) if rows else None)
            for name, counts in by_horizon.items()
        },
        "contains_predictive_result": False,
    }
