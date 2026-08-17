"""Stage 1 feature storage: compact derived state on disk, with provenance.

562 million raw MBO records are not going into Postgres, and neither is a
row-per-event derivative of them. What lands on disk is the sampled snapshot
set -- four cadences per symbol-day -- written as Parquet and accompanied by a
manifest that says exactly what produced it.

Provenance is recorded per symbol-day so any snapshot can be traced back to the
bytes it came from:

* the source DBN filename, its size, and its SHA-256
* the validator version that certified the reconstruction
* the feature-engine version and the frozen vocabulary hash
* the cadences, the row counts, and the time span actually covered

The vocabulary hash is the part that matters most. Stage 1 froze its feature
list before any predictive outcome was inspected; recording the hash in every
manifest means a vocabulary that changed after results were seen is visible in
the data rather than a matter of recollection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.services.mbo_book_validator import MBO_VALIDATOR_VERSION
from app.services.mbo_feature_engine import (
    CADENCES,
    FEATURE_ENGINE_VERSION,
    FEATURE_VOCABULARY,
    FEATURE_VOCABULARY_HASH,
    SNAPSHOT_COLUMNS,
    Cadence,
    OrderBookFeatureEngine,
    feature_definitions,
)

FEATURE_STORE_VERSION = "tier1_mbo_feature_store_v1"

# Rows are flushed in batches so a session is never fully resident.
DEFAULT_ROW_GROUP = 50_000

_INT_COLUMNS = {
    "ts_event",
    "sequence",
    "flast_index",
    "sequence_index",
    "window_ns",
    "window_flast_events",
    "window_records",
    "bid_size_l1",
    "ask_size_l1",
    "bid_order_count_l1",
    "ask_order_count_l1",
    "bid_depth_5",
    "ask_depth_5",
    "bid_depth_10",
    "ask_depth_10",
    "bid_order_count_5",
    "ask_order_count_5",
    "bid_levels",
    "ask_levels",
    "resting_orders",
    "best_bid_price",
    "best_ask_price",
    "spread",
    "add_count",
    "add_volume",
    "cancel_count",
    "cancel_volume",
    "modify_count",
    "execution_count",
    "execution_volume",
    "touch_replenishment_volume",
    "touch_replenishment_events",
    "queue_depletion_events",
    "best_bid_changes",
    "best_ask_changes",
    "trade_count",
    "trade_volume",
    "buy_aggressor_volume",
    "sell_aggressor_volume",
    "unclassified_trade_volume",
    "signed_trade_volume",
    "executions_without_price_move",
    "execution_volume_without_price_move",
    "refill_after_execution_volume",
    "depletion_followed_by_quote_move",
}

_STRING_COLUMNS = {"symbol", "session_date", "cadence"}


def snapshot_schema() -> pa.Schema:
    """One schema for every cadence, so files are trivially concatenable."""
    fields = []
    for column in SNAPSHOT_COLUMNS:
        if column in _STRING_COLUMNS:
            fields.append(pa.field(column, pa.string()))
        elif column in _INT_COLUMNS:
            fields.append(pa.field(column, pa.int64()))
        else:
            fields.append(pa.field(column, pa.float64()))
    return pa.schema(fields)


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Hash a source file without reading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class CadenceWriter:
    """One Parquet file per (symbol-day, cadence), written in row groups."""

    path: Path
    schema: pa.Schema
    row_group: int = DEFAULT_ROW_GROUP

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = pq.ParquetWriter(self.path, self.schema, compression="zstd")
        self._buffer: list[dict[str, Any]] = []
        self.rows = 0
        self.first_ts: int | None = None
        self.last_ts: int | None = None

    def add(self, row: dict[str, Any]) -> None:
        self._buffer.append(row)
        self.rows += 1
        if self.first_ts is None:
            self.first_ts = row["ts_event"]
        self.last_ts = row["ts_event"]
        if len(self._buffer) >= self.row_group:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        columns = {
            field.name: pa.array(
                [row.get(field.name) for row in self._buffer], type=field.type
            )
            for field in self.schema
        }
        self._writer.write_table(pa.Table.from_pydict(columns, schema=self.schema))
        self._buffer.clear()

    def close(self) -> None:
        self._flush()
        self._writer.close()


def write_session_features(
    events: Iterable[Any],
    *,
    symbol: str,
    session_date: str,
    output_dir: Path,
    source_path: Path | None = None,
    cadences: tuple[Cadence, ...] = CADENCES,
    row_group: int = DEFAULT_ROW_GROUP,
) -> dict[str, Any]:
    """Stream one symbol-day through the engine and write its snapshots.

    Returns the manifest. The event stream is consumed once and never held.
    """
    engine = OrderBookFeatureEngine(
        symbol=symbol, session_date=session_date, cadences=cadences
    )
    schema = snapshot_schema()
    writers: dict[str, CadenceWriter] = {}
    stem = f"{symbol}_{session_date}"
    try:
        for row in engine.process(events):
            cadence = row["cadence"]
            writer = writers.get(cadence)
            if writer is None:
                writer = CadenceWriter(
                    output_dir / cadence / f"{stem}.{cadence}.parquet",
                    schema,
                    row_group=row_group,
                )
                writers[cadence] = writer
            writer.add(row)
    finally:
        for writer in writers.values():
            writer.close()

    per_cadence = {
        name: {
            "path": str(writer.path.relative_to(output_dir)),
            "rows": writer.rows,
            "bytes": writer.path.stat().st_size if writer.path.exists() else 0,
            "first_ts_event": writer.first_ts,
            "last_ts_event": writer.last_ts,
        }
        for name, writer in sorted(writers.items())
    }

    manifest: dict[str, Any] = {
        "feature_store_version": FEATURE_STORE_VERSION,
        "feature_engine_version": FEATURE_ENGINE_VERSION,
        "validator_version": MBO_VALIDATOR_VERSION,
        "feature_vocabulary_hash": FEATURE_VOCABULARY_HASH,
        "feature_count": len(FEATURE_VOCABULARY),
        "symbol": symbol,
        "session_date": session_date,
        "generated_at": datetime.now(UTC).isoformat(),
        "records_consumed": engine._records,
        "flast_events": engine._flast_index,
        "cadences": per_cadence,
        "total_rows": sum(entry["rows"] for entry in per_cadence.values()),
        "total_bytes": sum(entry["bytes"] for entry in per_cadence.values()),
        "contains_forward_information": False,
    }
    if source_path is not None:
        manifest["source"] = {
            "filename": source_path.name,
            "bytes": source_path.stat().st_size,
            "sha256": sha256_file(source_path),
        }
    (output_dir / "manifests").mkdir(parents=True, exist_ok=True)
    (output_dir / "manifests" / f"{stem}.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def iter_manifests(output_dir: Path) -> Iterator[dict[str, Any]]:
    for path in sorted((output_dir / "manifests").glob("*.manifest.json")):
        yield json.loads(path.read_text(encoding="utf-8"))


def batch_manifest(output_dir: Path, manifests: list[dict[str, Any]]) -> dict[str, Any]:
    """One record describing the whole extraction, plus the definitions."""
    hashes = {m["feature_vocabulary_hash"] for m in manifests}
    engines = {m["feature_engine_version"] for m in manifests}
    summary = {
        "feature_store_version": FEATURE_STORE_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "symbol_days": len(manifests),
        "total_rows": sum(m["total_rows"] for m in manifests),
        "total_bytes": sum(m["total_bytes"] for m in manifests),
        "total_records_consumed": sum(m["records_consumed"] for m in manifests),
        "total_flast_events": sum(m["flast_events"] for m in manifests),
        "rows_by_cadence": {
            cadence.name: sum(
                m["cadences"].get(cadence.name, {}).get("rows", 0) for m in manifests
            )
            for cadence in CADENCES
        },
        "bytes_by_cadence": {
            cadence.name: sum(
                m["cadences"].get(cadence.name, {}).get("bytes", 0) for m in manifests
            )
            for cadence in CADENCES
        },
        # A single hash across every manifest is the check that the vocabulary
        # did not move mid-extraction.
        "feature_vocabulary_hashes": sorted(hashes),
        "feature_vocabulary_consistent": len(hashes) <= 1,
        "feature_engine_versions": sorted(engines),
        "definitions": feature_definitions(),
        "contains_forward_information": False,
    }
    (output_dir / "batch_manifest.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary


def estimate_storage(
    *,
    symbol_days: int,
    observed_rows: int,
    observed_bytes: int,
) -> dict[str, Any]:
    """Extrapolate full-batch storage from what has actually been written.

    Deliberately takes measured rows and bytes rather than assuming a
    bytes-per-row: Parquet with zstd compresses these columns well, and a
    guessed constant would be wrong by a large factor in either direction.
    """
    if symbol_days <= 0 or observed_rows <= 0:
        return {"measurable": False}
    bytes_per_row = observed_bytes / observed_rows
    return {
        "measurable": True,
        "observed_symbol_days": symbol_days,
        "observed_rows": observed_rows,
        "observed_bytes": observed_bytes,
        "bytes_per_row": round(bytes_per_row, 2),
        "rows_per_symbol_day": round(observed_rows / symbol_days, 1),
        "projected_160_symbol_day_rows": round(observed_rows / symbol_days * 160),
        "projected_160_symbol_day_bytes": round(observed_bytes / symbol_days * 160),
        "projected_160_symbol_day_gib": round(
            observed_bytes / symbol_days * 160 / (1024**3), 3
        ),
    }
