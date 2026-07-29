"""Locked, forward-only confirmation of the dataset-36 gap-down short lead."""

from __future__ import annotations

import argparse
import json

from app.db import connect
from app.services.intraday_factor_diagnostics import load_cost_model
from app.cli.intraday_factor_audit import _cost_calibration_id
from app.services.signal_diagnostics import (
    family_signal_diagnostics,
    persist_signal_diagnostics,
)

ARCHITECTURE = "gap_down_acceptance_short_confirmation_v1"
SOURCE_DATASET_ID = 36


def _progress(progress: dict) -> None:
    print(
        (
            f"work {progress['completed']}/{progress['total']} | "
            f"symbol {progress['symbol_index']}/{progress['symbols']} "
            f"{progress['symbol']}"
        ),
        flush=True,
    )


def execute(args: argparse.Namespace) -> dict:
    with connect() as conn:
        source = conn.execute(
            "SELECT window_end FROM research_dataset_manifests WHERE id = %s",
            (SOURCE_DATASET_ID,),
        ).fetchone()
        if not source or source["window_end"] is None:
            raise ValueError("Source dataset 36 with a fixed window_end is required.")
        dataset_id = args.dataset_id
        if dataset_id is None:
            row = conn.execute(
                """
                SELECT id FROM research_dataset_manifests
                WHERE dataset_kind = 'intraday' AND id <> %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (SOURCE_DATASET_ID,),
            ).fetchone()
            if not row:
                raise ValueError("No later intraday snapshot exists.")
            dataset_id = int(row["id"])
        if dataset_id == SOURCE_DATASET_ID:
            raise ValueError("Confirmation cannot reuse source dataset 36.")
        manifest = conn.execute(
            "SELECT assets, window_end FROM research_dataset_manifests WHERE id = %s",
            (dataset_id,),
        ).fetchone()
        if not manifest:
            raise ValueError(f"No dataset manifest for id={dataset_id}.")
        if manifest["window_end"] is None or manifest["window_end"] <= source["window_end"]:
            raise ValueError("The selected dataset has no sessions later than dataset 36.")
        symbols = [str(item).upper() for item in (manifest["assets"] or [])][: args.max_symbols]
        cost_model = load_cost_model(conn, _cost_calibration_id(conn, args.cost_calibration_id))
        stressed_cost = cost_model.get("stressed_round_trip_bps")
        if stressed_cost is None:
            stressed_cost = cost_model["conservative_round_trip_bps"]
        report = family_signal_diagnostics(
            conn,
            architecture=ARCHITECTURE,
            timeframe="30m",
            dataset_id=dataset_id,
            symbols=symbols,
            max_variants=1,
            horizons=(1,),
            minimum_timestamp_exclusive=source["window_end"],
            cost_bps_override=float(stressed_cost),
            progress_callback=_progress,
        )
        report["source_dataset_id"] = SOURCE_DATASET_ID
        report["cost_model"] = cost_model
        report["forward_only"] = True
        if not args.no_persist:
            try:
                persisted = persist_signal_diagnostics(conn, report)
                conn.commit()
                report["persistence"] = {"status": "stored", "id": persisted["id"]}
            except Exception as error:  # noqa: BLE001 - never hide an expensive computed result
                conn.rollback()
                report["persistence"] = {"status": "failed", "error": str(error)}
        return report


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Confirm the exact dataset-36 gap-down acceptance short on later dates only. "
            "No campaign or broker action."
        )
    )
    root.add_argument("--dataset-id", type=int)
    root.add_argument("--max-symbols", type=int, default=100)
    root.add_argument("--cost-calibration-id", help="Calibration integer id, or 'latest'.")
    root.add_argument("--no-persist", action="store_true")
    return root


def main() -> None:
    print("Locked gap-down confirmation | 30m | forward dates only", flush=True)
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()
