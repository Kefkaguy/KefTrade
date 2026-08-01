"""VPS CLI for the back half of the governed cycle.

Phases 6-9 existed as a library with no way to run them: a confirmed factor
could not be turned into a family, a family could not be simulated, and the
elite gates had nothing to judge.  This is that entry point.

The order is not negotiable and each command refuses to run ahead of its
evidence.  A family cannot be frozen without a passing locked confirmation, a
simulation cannot run without a frozen family, and qualification cannot be
claimed without every reference in the chain.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.db import connect
from app.services.intraday_elite_gates import (
    ELITE_GATES_VERSION,
    FamilyRecipe,
    execution_semantics_report,
    fill_calibration_report,
    freeze_family,
    persist_elite_qualification,
    qualify_elite,
    robustness_report,
)
from app.services.intraday_sector_backfill import backfill_sectors
from app.services.intraday_strategy_simulation import simulate_family


def _slots(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def sectors(args: argparse.Namespace) -> dict[str, Any]:
    """Fill the sector holes that leave the peer-group family unmeasurable."""
    with connect() as conn:
        return backfill_sectors(
            conn,
            universe_key=args.universe_key,
            symbols=_slots(args.symbols) or None,
            limit=args.limit,
        )


def _confirmation_run(conn: Any, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, mode, timeframe, dataset_id, factor_keys, results,
               certification_id, declaration_id
        FROM intraday_factor_diagnostic_runs
        WHERE id = %s
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"No factor run {run_id}.")
    if str(row["mode"]) != "confirmation":
        raise ValueError(
            f"Run {run_id} is a {row['mode']} run. A family may only be built on "
            "a locked-confirmation run; building on discovery is how a "
            "validation set becomes a training set."
        )
    return dict(row)


def freeze(args: argparse.Namespace) -> dict[str, Any]:
    """Phase 5: one confirmed factor becomes one deterministic family."""
    with connect() as conn:
        confirmation = _confirmation_run(conn, args.confirmation_run_id)
        passed = list((confirmation["results"] or {}).get("passed_locked_confirmation") or [])
        if not passed:
            return {
                "frozen": False,
                "confirmation_run_id": args.confirmation_run_id,
                "detail": (
                    "no factor passed locked confirmation in this run; there is "
                    "nothing to build a family on"
                ),
            }
        recipe = FamilyRecipe(
            factor_key=args.factor_key,
            entry_condition=args.entry_condition,
            direction=args.direction,
            holding_bars=args.holding_bars,
            stop_loss=args.stop_loss,
            forced_session_close_exit=True,
            max_concurrent_positions=args.max_concurrent_positions,
            position_size_fraction=args.position_size_fraction,
            max_gross_exposure=args.max_gross_exposure,
            eligible_symbols=_slots(args.eligible_symbols),
            eligible_session_slots=_slots(args.eligible_session_slots),
            cost_calibration_id=args.cost_calibration_id,
        )
        result = freeze_family(
            conn, recipe=recipe, confirmation_run_id=args.confirmation_run_id
        )
        return {**result, "frozen": True, "recipe": recipe.frozen()}


def _family(conn: Any, family_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, factor_key, recipe, recipe_hash, confirmation_run_id,
               cost_calibration_id
        FROM intraday_strategy_families WHERE id = %s
        """,
        (family_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"No strategy family {family_id}.")
    return dict(row)


def simulate(args: argparse.Namespace) -> dict[str, Any]:
    """Phase 6: executable simulation, then every robustness question."""
    from app.cli.intraday_factor_audit import (
        TRADE_FLOW_FEEDS,
        _dataset_source,
        _load_premarket,
        _load_trade_flow,
    )
    from app.services.intraday_factor_diagnostics import (
        FACTOR_SPECS,
        load_cost_model,
        load_dataset_candles,
        sector_map,
    )

    with connect() as conn:
        family = _family(conn, args.family_id)
        stored = family["recipe"] or {}
        recipe = FamilyRecipe(
            factor_key=str(family["factor_key"]),
            entry_condition=str(stored["entry_condition"]),
            direction=str(stored["direction"]),
            holding_bars=int(stored["holding_bars"]),
            stop_loss=str(stored["stop_loss"]),
            forced_session_close_exit=bool(stored["forced_session_close_exit"]),
            max_concurrent_positions=int(stored["max_concurrent_positions"]),
            position_size_fraction=float(stored["position_size_fraction"]),
            max_gross_exposure=float(stored["max_gross_exposure"]),
            eligible_symbols=tuple(stored.get("eligible_symbols") or ()),
            eligible_session_slots=tuple(stored.get("eligible_session_slots") or ()),
            cost_calibration_id=int(stored["cost_calibration_id"]),
        )
        spec = FACTOR_SPECS[recipe.factor_key]
        cost_model = load_cost_model(conn, calibration_id=recipe.cost_calibration_id)

        symbols = list(recipe.eligible_symbols) or None
        candles, _manifest = load_dataset_candles(
            conn,
            dataset_id=args.dataset_id,
            timeframe=args.timeframe,
            symbols=symbols,
            max_symbols=args.max_symbols,
            include_benchmarks=False,
        )
        print(f"simulation: {len(candles)} symbols loaded", flush=True)

        # The family is simulated on exactly the channels its factor was
        # confirmed on; a missing one would quietly produce zero trades.
        observations = spec.builder(
            candles,
            timeframe=args.timeframe,
            sector_by_symbol=sector_map(conn, list(candles)),
            premarket_by_symbol=_load_premarket(
                conn,
                list(candles),
                timeframe=args.timeframe,
                source=_dataset_source(conn, dataset_id=args.dataset_id),
            )
            or None,
            trade_flow_by_symbol=_load_trade_flow(
                conn,
                list(candles),
                timeframe=args.timeframe,
                feed=TRADE_FLOW_FEEDS[_dataset_source(conn, dataset_id=args.dataset_id)],
            )
            or None,
        )
        print(f"simulation: {len(observations)} observations", flush=True)

        result = simulate_family(
            observations,
            recipe=recipe,
            candles_by_symbol=candles,
            cost_model=cost_model,
            capital=args.capital,
        )
        trades = result["trades"]
        execution = execution_semantics_report(trades)
        robustness = robustness_report(
            trades, max_drawdown_bps=args.max_drawdown_bps
        )

        simulation_id = conn.execute(
            """
            INSERT INTO intraday_strategy_simulations(
                family_id, factor_key, timeframe, dataset_id, recipe_hash,
                cost_calibration_id, capital, trade_count, observations,
                fill_rate, execution_passed, robustness_passed,
                execution_report, robustness_report, trades, simulation_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                args.family_id,
                recipe.factor_key,
                args.timeframe,
                args.dataset_id,
                recipe.recipe_hash(),
                recipe.cost_calibration_id,
                args.capital,
                result["trade_count"],
                result["observations"],
                result["fill_rate"],
                bool(execution["passed"]),
                bool(robustness["passed"]),
                _json(execution),
                _json(robustness),
                _json(trades[: args.store_trades]),
                result["simulation_version"],
            ),
        ).fetchone()
        conn.commit()

        return {
            "simulation_run_id": int(simulation_id["id"]),
            "family_id": args.family_id,
            "factor_key": recipe.factor_key,
            "observations": result["observations"],
            "trade_count": result["trade_count"],
            "fill_rate": result["fill_rate"],
            "skipped": result["skipped"],
            "execution": execution,
            "robustness": robustness,
        }


def _json(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    from app.services.research_architecture import jsonable

    return Jsonb(jsonable(value))


def calibrate_fills(args: argparse.Namespace) -> dict[str, Any]:
    """Phase 7: what execution actually charged, against what was confirmed."""
    with connect() as conn:
        fills = [
            dict(row)
            for row in conn.execute(
                """
                SELECT filled_price, midpoint_at_decision, bid, ask, side,
                       symbol, decision_timestamp, filled_at, quantity
                FROM intraday_paper_fill_observations
                WHERE factor_key = %s
                ORDER BY decision_timestamp
                """,
                (args.factor_key,),
            ).fetchall()
        ]
        report = fill_calibration_report(
            fills,
            confirmed_gross_edge_bps=args.confirmed_gross_edge_bps,
            research_signals_per_session=args.research_signals_per_session,
        )
        row = conn.execute(
            """
            INSERT INTO intraday_fill_calibrations(
                family_id, factor_key, timeframe, matched_fills, passed,
                report, gates_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                args.family_id,
                args.factor_key,
                args.timeframe,
                int(report.get("matched_fills") or 0),
                bool(report.get("passed")),
                _json(report),
                ELITE_GATES_VERSION,
            ),
        ).fetchone()
        conn.commit()
        return {**report, "fill_calibration_id": int(row["id"])}


def qualify(args: argparse.Namespace) -> dict[str, Any]:
    """Phase 8: every gate must hold and every stage must be referenceable."""
    with connect() as conn:
        simulation = conn.execute(
            """
            SELECT s.*, f.confirmation_run_id
            FROM intraday_strategy_simulations s
            JOIN intraday_strategy_families f ON f.id = s.family_id
            WHERE s.id = %s
            """,
            (args.simulation_run_id,),
        ).fetchone()
        if not simulation:
            raise ValueError(f"No simulation {args.simulation_run_id}.")
        simulation = dict(simulation)

        confirmation = _confirmation_run(conn, int(simulation["confirmation_run_id"]))
        discovery = conn.execute(
            """
            SELECT id, results FROM intraday_factor_diagnostic_runs
            WHERE mode = 'discovery' AND dataset_id = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (simulation["dataset_id"],),
        ).fetchone()
        quality = conn.execute(
            """
            SELECT id, ready_for_discovery, power_passed
            FROM intraday_dataset_quality_reports
            WHERE dataset_id = %s AND timeframe = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (simulation["dataset_id"], simulation["timeframe"]),
        ).fetchone()
        manifest = conn.execute(
            "SELECT content_hash FROM research_dataset_manifests WHERE id = %s",
            (simulation["dataset_id"],),
        ).fetchone()
        hypothesis = conn.execute(
            """
            SELECT id FROM intraday_research_hypotheses
            WHERE factor_key = %s ORDER BY version DESC LIMIT 1
            """,
            (simulation["factor_key"],),
        ).fetchone()
        trials = conn.execute(
            "SELECT COUNT(*) AS trials FROM intraday_research_trial_declarations"
        ).fetchone()

        passed_confirmation = simulation["factor_key"] in list(
            (confirmation["results"] or {}).get("passed_locked_confirmation") or []
        )
        passed_discovery = simulation["factor_key"] in list(
            ((discovery or {}).get("results") or {}).get("evidence_survivors") or []
        )

        evidence = {
            "certification_id": confirmation.get("certification_id"),
            "declaration_id": confirmation.get("declaration_id"),
            "hypothesis_id": (hypothesis or {}).get("id"),
            "dataset_id": simulation["dataset_id"],
            "dataset_hash": (manifest or {}).get("content_hash"),
            "quality_report_id": (quality or {}).get("id"),
            "discovery_run_id": (discovery or {}).get("id"),
            "confirmation_run_id": simulation["confirmation_run_id"],
            "cost_calibration_id": simulation["cost_calibration_id"],
            "family_id": simulation["family_id"],
            "simulation_run_id": simulation["id"],
            "fill_calibration_id": args.fill_calibration_id,
            "cumulative_trial_count": int((trials or {}).get("trials") or 0),
        }
        verdict = qualify_elite(
            evidence=evidence,
            discovery_passed=passed_discovery,
            confirmation_passed=passed_confirmation,
            quality_report=dict(quality or {}),
            execution_report=simulation["execution_report"] or {},
            robustness=simulation["robustness_report"] or {},
            fill_calibration=_fill_calibration(conn, args.fill_calibration_id),
            # Risk approval is a human decision and is never inferred from a
            # passing metric.
            risk_approved=bool(args.risk_approved),
        )
        verdict["qualification_id"] = persist_elite_qualification(
            conn,
            factor_key=str(simulation["factor_key"]),
            timeframe=str(simulation["timeframe"]),
            verdict=verdict,
        )
        return verdict


def _fill_calibration(conn: Any, calibration_id: int | None) -> dict[str, Any]:
    if calibration_id is None:
        return {"passed": False, "detail": "no fill calibration referenced"}
    row = conn.execute(
        "SELECT report FROM intraday_fill_calibrations WHERE id = %s",
        (calibration_id,),
    ).fetchone()
    return dict((row or {}).get("report") or {"passed": False})


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Strategy construction, execution simulation and elite "
            "qualification for a confirmed intraday factor."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    sector_command = commands.add_parser(
        "sectors", help="Backfill missing sector classification."
    )
    sector_command.add_argument("--universe-key")
    sector_command.add_argument("--symbols")
    sector_command.add_argument("--limit", type=int)

    freeze_command = commands.add_parser(
        "freeze", help="Freeze one confirmed factor into one deterministic family."
    )
    freeze_command.add_argument("--confirmation-run-id", type=int, required=True)
    freeze_command.add_argument("--factor-key", required=True)
    freeze_command.add_argument("--entry-condition", required=True)
    freeze_command.add_argument(
        "--direction", required=True, choices=("long", "short", "both")
    )
    freeze_command.add_argument("--holding-bars", type=int, required=True)
    freeze_command.add_argument("--stop-loss", default="none; horizon exit only")
    freeze_command.add_argument("--max-concurrent-positions", type=int, default=5)
    freeze_command.add_argument("--position-size-fraction", type=float, default=0.1)
    freeze_command.add_argument("--max-gross-exposure", type=float, default=0.5)
    freeze_command.add_argument("--eligible-symbols")
    freeze_command.add_argument("--eligible-session-slots")
    freeze_command.add_argument("--cost-calibration-id", type=int, required=True)

    simulate_command = commands.add_parser(
        "simulate", help="Executable simulation and the full robustness report."
    )
    simulate_command.add_argument("--family-id", type=int, required=True)
    simulate_command.add_argument("--dataset-id", type=int, required=True)
    simulate_command.add_argument("--timeframe", default="30m", choices=("15m", "30m"))
    simulate_command.add_argument("--capital", type=float, default=1_000_000.0)
    simulate_command.add_argument("--max-symbols", type=int, default=300)
    simulate_command.add_argument("--max-drawdown-bps", type=float)
    simulate_command.add_argument(
        "--store-trades",
        type=int,
        default=5000,
        help="Trades persisted with the simulation; the reports read them all.",
    )

    fills_command = commands.add_parser(
        "calibrate-fills", help="Turn observed paper fills into a cost verdict."
    )
    fills_command.add_argument("--factor-key", required=True)
    fills_command.add_argument("--family-id", type=int)
    fills_command.add_argument("--timeframe", default="30m", choices=("15m", "30m"))
    fills_command.add_argument(
        "--confirmed-gross-edge-bps", type=float, required=True
    )
    fills_command.add_argument("--research-signals-per-session", type=float)

    qualify_command = commands.add_parser(
        "qualify", help="Judge every elite gate against the whole evidence chain."
    )
    qualify_command.add_argument("--simulation-run-id", type=int, required=True)
    qualify_command.add_argument("--fill-calibration-id", type=int)
    qualify_command.add_argument(
        "--risk-approved",
        action="store_true",
        help="A human risk sign-off. Never inferred from a passing metric.",
    )

    return root


COMMANDS = {
    "sectors": sectors,
    "freeze": freeze,
    "simulate": simulate,
    "calibrate-fills": calibrate_fills,
    "qualify": qualify,
}


def main() -> None:
    args = parser().parse_args()
    print("Intraday strategy pipeline | backend only | research use", flush=True)
    print(json.dumps(COMMANDS[args.command](args), default=str, indent=2))


if __name__ == "__main__":
    main()
