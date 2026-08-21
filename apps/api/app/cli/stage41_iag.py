"""Stage 4.1 CLI: IAG-v1 diagnose, and the one gated economic reveal.

``plan`` and ``semantics`` need no data. ``diagnose`` reads the frozen feature
files and the news population, measures every qualification variable inside
``[t0, t0+120s]``, and selects the specification from counts alone -- it cannot
compute an outcome because it never imports the function that would.

``run`` is the Stage-4.2 reveal. It moves the ledger 531 -> 532, once, for the
specification ``diagnose`` already committed to.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.stage41_iag_plan import (
    CERTIFIED_SYMBOLS,
    DIAGNOSTIC_FILENAME,
    DIRECTIONAL_FEATURES,
    EFFECTIVE_TRIALS_AFTER_DESIGN,
    EFFECTIVE_TRIALS_AFTER_REVEAL,
    EFFECTIVE_TRIALS_BEFORE,
    MIN_BASELINE_TILES,
    MIN_ROWS_CONFIRMING,
    MIN_ROWS_PRIMARY,
    NANOS_PER_MINUTE,
    NON_DIRECTIONAL_FEATURES,
    OBSERVATION_NS,
    OBSERVATION_SECONDS,
    PRIMARY_HORIZON_MINUTES,
    QUIET_PERIOD_MINUTES,
    REPORT_RELATIVE_DIR,
    RESULTS_FILENAME,
    SECONDARY_HORIZON_MINUTES,
    SELECTION_FILENAME,
    SIDE_AGNOSTIC_FORBIDDEN_FOR_DIRECTION,
    STAGE41_AMENDMENT,
    STAGE41_PLAN_VERSION,
    UNUSABLE_FEATURES,
    assert_frozen_design,
    statistical_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT_DIR = REPO_ROOT / REPORT_RELATIVE_DIR

# Tokens that would signal an economic outcome leaking into an outcome-blind
# artifact. Same defence as Stage 3.5/3.6: a filter over emitted keys, backed by
# structural tests that the quantity is never computed in the first place.
OUTCOME_BEARING_TOKENS: tuple[str, ...] = (
    "gross_bps",
    "displacement_bps",
    "return",
    "pnl",
    "profit",
    "clustered_t",
    "ci95",
    "horizon_midpoint",
)
OUTCOME_TOKEN_EXEMPTIONS: tuple[str, ...] = (
    "contains_post_decision_return",
    "contains_pnl",
    "primary_gross_hurdle_bps",
    "stretch_gross_hurdle_bps",
)


def _strip_outcomes(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove anything that could be an economic outcome, by key name."""
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        exempt = any(token in lowered for token in OUTCOME_TOKEN_EXEMPTIONS)
        if not exempt and any(token in lowered for token in OUTCOME_BEARING_TOKENS):
            continue
        if isinstance(value, dict):
            clean[key] = _strip_outcomes(value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            clean[key] = [_strip_outcomes(v) for v in value]
        else:
            clean[key] = value
    return clean


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _governance(*, revealed: bool) -> dict[str, Any]:
    from app.services.stage41_iag_executor import STAGE41_EXECUTOR_VERSION

    return {
        "stage41_plan_version": STAGE41_PLAN_VERSION,
        "stage41_amendment": STAGE41_AMENDMENT,
        "stage41_executor_version": STAGE41_EXECUTOR_VERSION,
        "contains_strategy_outcome": revealed,
        "contains_post_decision_return": revealed,
        "contains_pnl": False,
        "effective_trials_before": EFFECTIVE_TRIALS_BEFORE,
        "effective_trials_after": (
            EFFECTIVE_TRIALS_AFTER_REVEAL if revealed else EFFECTIVE_TRIALS_AFTER_DESIGN
        ),
        "authorizes_paper_or_live": False,
    }


def plan(args: argparse.Namespace) -> dict[str, Any]:
    """The frozen plan, with the design hash verified."""
    design = assert_frozen_design(REPO_ROOT)
    payload = {**_governance(revealed=False), "design": design, **statistical_plan()}
    _write(Path(args.output_dir) / "stage41_plan.json", payload)
    return payload


def semantics(_args: argparse.Namespace) -> dict[str, Any]:
    """What each feature actually measures. Needs no data."""
    return {
        **_governance(revealed=False),
        "directional": [
            {"name": f.name, "kind": f.kind, "aggregation": f.aggregation, "note": f.note}
            for f in DIRECTIONAL_FEATURES
        ],
        "regime_descriptors": [
            {"name": f.name, "kind": f.kind, "aggregation": f.aggregation, "note": f.note}
            for f in NON_DIRECTIONAL_FEATURES
        ],
        "forbidden_as_directional_evidence": list(SIDE_AGNOSTIC_FORBIDDEN_FOR_DIRECTION),
        "forbidden_reason": (
            "each accumulates both book sides into a single counter, so it "
            "cannot distinguish depletion on the informed side from depletion "
            "on the opposite side"
        ),
        "unusable": list(UNUSABLE_FEATURES),
        "impacted_side_rule": {"+1": "ask_depth_10", "-1": "bid_depth_10"},
    }


# ---------------------------------------------------------------------------
# Measurement shared by diagnose (and, for the selected spec only, by run)
# ---------------------------------------------------------------------------


def _news_events(args: argparse.Namespace) -> list[dict[str, Any]]:
    """The Stage-4.0 population: isolated, L3-covered, certified symbols only.

    Reuses the Stage-4.0 machinery rather than re-deriving isolation, so the two
    stages cannot disagree about which events exist.
    """
    from app.cli.stage40_audit import _certified_sessions, _open_connection
    from app.db import connect
    from app.services.stage40_audit import load_l3_coverage

    features_dir = Path(args.features_dir)
    if not features_dir.is_dir():
        raise ValueError(f"the Stage-1 feature directory is missing at {features_dir}")
    sessions = _certified_sessions()
    coverage = load_l3_coverage(
        features_dir, symbols=CERTIFIED_SYMBOLS, sessions=sessions
    )

    with _open_connection(connect) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT symbol,
                   COALESCE(content_hash, article_id) AS story_id,
                   MIN(known_at) AS known_at
              FROM intraday_news_articles
             WHERE known_at >= %s AND known_at < %s AND symbol = ANY(%s)
             GROUP BY symbol, COALESCE(content_hash, article_id)
             ORDER BY symbol, MIN(known_at)
            """,
            ["2025-06-02 00:00:00+00", "2025-07-01 00:00:00+00", list(CERTIFIED_SYMBOLS)],
        )
        rows = list(cursor.fetchall() or [])

    from datetime import timedelta

    from app.services.stage40_audit import _epoch_nanoseconds

    quiet = timedelta(minutes=QUIET_PERIOD_MINUTES)
    last_seen: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    for row in rows:
        symbol, moment = row["symbol"], row["known_at"]
        previous = last_seen.get(symbol)
        isolated = previous is None or (moment - previous) >= quiet
        last_seen[symbol] = moment
        if not isolated:
            continue
        day = moment.date().isoformat()
        instant = _epoch_nanoseconds(moment)
        if not coverage.covers(symbol, day, instant):
            continue
        events.append(
            {
                "symbol": symbol,
                "session_date": day,
                "story_id": row["story_id"],
                "t0_ns": instant,
            }
        )
    return events


def _measure_states(args: argparse.Namespace, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure every event's IAG state. Reads nothing after ``t_obs_end``."""
    from app.services.stage41_iag_executor import (
        CONFIRMING_CADENCE,
        PRIMARY_CADENCE,
        build_baseline,
        feature_path,
        load_feature_columns,
        measure_event,
        tile_prior_window,
    )

    features_dir = Path(args.features_dir)
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_symbol.setdefault(event["symbol"], []).append(event)

    states = []
    baselines: list[dict[str, Any]] = []
    skipped_missing_features = 0

    for index, symbol in enumerate(sorted(by_symbol), start=1):
        symbol_events = sorted(by_symbol[symbol], key=lambda e: e["t0_ns"])
        sessions = sorted({e["session_date"] for e in symbol_events})
        print(f"[{index}/{len(by_symbol)}] {symbol}", flush=True)

        # Baseline accumulates strictly prior tiles as the walk advances, so an
        # event is never scored against its own session's later data.
        tiles: list[Any] = []
        cache: dict[str, dict[str, Any]] = {}
        for session_date in sessions:
            primary_file = feature_path(features_dir, symbol, session_date, PRIMARY_CADENCE)
            confirming_file = feature_path(
                features_dir, symbol, session_date, CONFIRMING_CADENCE
            )
            if not primary_file.is_file() or not confirming_file.is_file():
                cache[session_date] = {}
                continue
            cache[session_date] = {
                PRIMARY_CADENCE: load_feature_columns(primary_file),
                CONFIRMING_CADENCE: load_feature_columns(confirming_file),
            }

        for session_date in sessions:
            columns = cache.get(session_date) or {}
            primary = columns.get(PRIMARY_CADENCE)
            confirming = columns.get(CONFIRMING_CADENCE)
            session_events = [e for e in symbol_events if e["session_date"] == session_date]
            if primary is None or confirming is None:
                skipped_missing_features += len(session_events)
                continue

            session_start = int(primary["feature_available_ts_recv"].min())
            for event in session_events:
                baseline = build_baseline(
                    symbol,
                    tiles
                    + tile_prior_window(
                        primary, start_ns=session_start, end_ns=event["t0_ns"]
                    ),
                )
                states.append(
                    measure_event(
                        symbol=symbol,
                        session_date=session_date,
                        story_id=event["story_id"],
                        t0_ns=event["t0_ns"],
                        primary=primary,
                        confirming=confirming,
                        baseline=baseline,
                    )
                )
            # Whole session folds into the baseline only once it is past.
            session_end = int(primary["feature_available_ts_recv"].max())
            tiles.extend(
                tile_prior_window(primary, start_ns=session_start, end_ns=session_end)
            )
            baselines.append({"symbol": symbol, "through": session_date, "tiles": len(tiles)})

    return {
        "states": states,
        "baseline_growth": baselines,
        "events_skipped_missing_features": skipped_missing_features,
    }


def diagnose(args: argparse.Namespace) -> dict[str, Any]:
    """Every gate the reveal depends on, and no economic outcome.

    This never imports ``gross_directional_displacement_bps``, so no
    post-``t_obs_end`` price can be read from here even by mistake. A structural
    test asserts that absence.
    """
    from app.services.stage41_iag_executor import (
        FAILURE_REASONS,
        select_specification,
        write_selection,
    )

    design = assert_frozen_design(REPO_ROOT)
    events = _news_events(args)
    measured = _measure_states(args, events)
    states = measured["states"]

    selection = select_specification(states)
    output_dir = Path(args.output_dir)
    selection_record = {
        "design_sha256": design["design"]["sha256"],
        "selected_specification": selection["selected_specification"],
        "selection_basis": selection["selection_basis"],
        "primary_counts": selection["primary"],
        "fallback_counts": selection["fallback"],
        "economic_run_authorized": selection["economic_run_authorized"],
    }
    selection_sha = write_selection(selection_record, output_dir / SELECTION_FILENAME)

    directions = [s.direction for s in states]
    payload = {
        **_governance(revealed=False),
        "design": design,
        "diagnostic_only": True,
        "candidate_events": len(events),
        "events_skipped_missing_features": measured["events_skipped_missing_features"],
        "observation_window_seconds": OBSERVATION_SECONDS,
        "feature_row_support": {
            "min_rows_200ev_required": MIN_ROWS_PRIMARY,
            "min_rows_50ev_required": MIN_ROWS_CONFIRMING,
            "events_meeting_row_minimum": sum(
                1
                for s in states
                if s.rows_primary >= MIN_ROWS_PRIMARY
                and s.rows_confirming >= MIN_ROWS_CONFIRMING
            ),
            "median_rows_200ev": _median([s.rows_primary for s in states]),
            "median_rows_50ev": _median([s.rows_confirming for s in states]),
        },
        "direction_consistency": {
            "unambiguous": sum(1 for d in directions if d is not None),
            "long": sum(1 for d in directions if d == 1),
            "short": sum(1 for d in directions if d == -1),
            "ambiguous": sum(1 for d in directions if d is None),
            "ambiguity_reasons": _tally(
                [s.direction_reason for s in states if s.direction_reason]
            ),
            "cadences_required_to_agree": ["50ev", "200ev"],
        },
        "causal_checks": {
            "all_states_measured_within_observation_window": all(
                s.t_obs_end_ns == s.t0_ns + OBSERVATION_NS for s in states
            ),
            "lambda_defined": sum(1 for s in states if s.lambda_value is not None),
            "lambda_undefined": sum(1 for s in states if s.lambda_value is None),
            "baseline_min_tiles_required": MIN_BASELINE_TILES,
            "events_with_sufficient_baseline": sum(
                1 for s in states if s.baseline_tiles >= MIN_BASELINE_TILES
            ),
            "no_post_t_obs_end_price_access": True,
        },
        "failure_reason_vocabulary": list(FAILURE_REASONS),
        "specification_selection": selection,
        "selection_record_sha256": selection_sha,
        "contains_stage_4_2_outcome": False,
    }
    clean = _strip_outcomes(payload)
    _write(output_dir / DIAGNOSTIC_FILENAME, clean)
    return clean


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _tally(values: list[str]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return [{"reason": k, "count": v} for k, v in sorted(counts.items())]


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The Stage-4.2 economic reveal. One specification, once.

    Gated three ways: an explicit flag, a persisted selection whose hash must
    still match, and a refusal to overwrite an existing result file. There is no
    ``--limit``; the frozen specification is one test over its whole eligible
    population.
    """
    from app.services.stage41_iag_executor import (
        PRIMARY_CADENCE,
        decide_verdict,
        feature_path,
        gross_directional_displacement_bps,
        load_feature_columns,
        qualifies,
        read_selection,
        session_clustered_inference,
        specification_by_name,
    )

    if not args.i_have_reviewed_the_design:
        raise ValueError(
            "the Stage-4.2 economic reveal is not authorized. It exposes the one "
            "primary IAG-v1 specification and advances the ledger "
            f"{EFFECTIVE_TRIALS_BEFORE} -> {EFFECTIVE_TRIALS_AFTER_REVEAL} whether "
            "it passes or fails; re-run with --i-have-reviewed-the-design once "
            "that review has happened."
        )

    design = assert_frozen_design(REPO_ROOT)
    output_dir = Path(args.output_dir)
    results_path = output_dir / RESULTS_FILENAME
    if results_path.exists():
        raise ValueError(
            f"a Stage-4.2 result already exists at {results_path}. The reveal is "
            "one-time; a second run against the same sample would be a new "
            "exploratory specification, not a re-confirmation."
        )

    selection = read_selection(
        output_dir / SELECTION_FILENAME, expected_sha256=args.selection_sha256
    )
    if selection.get("design_sha256") != design["design"]["sha256"]:
        raise ValueError(
            "the persisted selection was made against a different design "
            "document; re-run diagnose against the current frozen design"
        )
    if not selection.get("economic_run_authorized"):
        raise ValueError(
            "diagnose selected no specification: neither cleared the declared "
            "floors, so the verdict is insufficient sample and no economic run "
            "occurs."
        )
    spec = specification_by_name(selection["selected_specification"])

    events = _news_events(args)
    measured = _measure_states(args, events)
    eligible = [s for s in measured["states"] if qualifies(s, spec)[0]]

    features_dir = Path(args.features_dir)
    displacements: list[float] = []
    sessions: list[str] = []
    per_symbol: dict[str, list[float]] = {}
    per_session: dict[str, list[float]] = {}
    unresolved = 0

    all_horizons = (PRIMARY_HORIZON_MINUTES, *SECONDARY_HORIZON_MINUTES)
    horizons: dict[int, list[float]] = {minutes: [] for minutes in all_horizons}

    cache: dict[tuple[str, str], Any] = {}
    for state in eligible:
        key = (state.symbol, state.session_date)
        if key not in cache:
            path = feature_path(
                features_dir, state.symbol, state.session_date, PRIMARY_CADENCE
            )
            cache[key] = load_feature_columns(path) if path.is_file() else None
        columns = cache[key]
        if columns is None:
            unresolved += 1
            continue

        decision_mid = _midpoint_at(columns, state.t_obs_end_ns)
        if decision_mid is None:
            unresolved += 1
            continue

        # Every horizon is computed before any is recorded, so an event that
        # cannot resolve one of them contributes to none of them. A partially
        # recorded event would make the primary and secondary populations
        # differ, and the secondaries are only interpretable against the same
        # events the primary used.
        computed: dict[int, float] = {}
        for minutes in all_horizons:
            horizon_mid = _midpoint_at(
                columns, state.t_obs_end_ns + minutes * NANOS_PER_MINUTE
            )
            if horizon_mid is None:
                break
            computed[minutes] = gross_directional_displacement_bps(
                direction=state.direction,
                midpoint_at_decision=decision_mid,
                midpoint_at_horizon=horizon_mid,
            )
        if len(computed) != len(all_horizons):
            unresolved += 1
            continue

        for minutes, value in computed.items():
            horizons[minutes].append(value)
        primary_value = computed[PRIMARY_HORIZON_MINUTES]
        displacements.append(primary_value)
        sessions.append(state.session_date)
        per_symbol.setdefault(state.symbol, []).append(primary_value)
        per_session.setdefault(state.session_date, []).append(primary_value)

    inference = session_clustered_inference(displacements, sessions)
    verdict = decide_verdict(inference)

    report = {
        **_governance(revealed=True),
        "design": design,
        "specification": spec.name,
        "selection_basis": selection["selection_basis"],
        "eligible_events": len(eligible),
        "events_unresolved_at_horizon": unresolved,
        "primary_horizon_minutes": PRIMARY_HORIZON_MINUTES,
        "primary": inference,
        "verdict": verdict,
        "secondary_diagnostics": {
            f"{minutes}min": _summary(values)
            for minutes, values in horizons.items()
            if minutes != PRIMARY_HORIZON_MINUTES
        },
        "secondary_may_rescue_primary": False,
        "per_symbol_diagnostic": {k: _summary(v) for k, v in sorted(per_symbol.items())},
        "per_session_diagnostic": {k: _summary(v) for k, v in sorted(per_session.items())},
        "diagnostics_may_filter_population": False,
        "is_pnl": False,
        "is_executable_profit": False,
    }
    _write(results_path, report)
    return {k: v for k, v in report.items() if k != "per_session_diagnostic"}


def _midpoint_at(columns: Any, instant_ns: int) -> float | None:
    """The latest midpoint available at or before ``instant_ns``.

    Latest-available rather than nearest, so the value could actually have been
    observed at that moment. Returns None when nothing qualifies or the book was
    one-sided, which the caller counts as unresolved rather than guessing.
    """
    import numpy as np

    available = columns["feature_available_ts_recv"]
    mid = columns["midpoint"].astype(float)
    usable = np.flatnonzero((available <= instant_ns) & np.isfinite(mid))
    if usable.size == 0:
        return None
    chosen = usable[int(np.argmax(available[usable]))]
    value = float(mid[chosen])
    return value if value > 0 else None


def _summary(values: list[float]) -> dict[str, Any]:
    import numpy as np

    if not values:
        return {"events": 0, "mean": None, "median": None}
    array = np.asarray(values, dtype=float)
    return {
        "events": int(array.size),
        "mean": float(array.mean()),
        "median": float(np.median(array)),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-stage41-iag",
        description=(
            "Stage 4.1 IAG-v1: outcome-blind diagnose, and the single gated "
            "Stage-4.2 economic reveal."
        ),
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_cmd = subparsers.add_parser("plan", help="Emit the frozen IAG-v1 plan.")
    plan_cmd.set_defaults(handler=plan)

    sem_cmd = subparsers.add_parser(
        "semantics", help="Exact feature semantics; needs no data."
    )
    sem_cmd.set_defaults(handler=semantics)

    def add_inputs(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--features-dir", required=True)

    diag = subparsers.add_parser(
        "diagnose", help="Every gate the reveal depends on; exposes no outcome."
    )
    add_inputs(diag)
    diag.set_defaults(handler=diagnose)

    reveal = subparsers.add_parser(
        "run", help="The Stage-4.2 economic reveal (gated, one-time)."
    )
    add_inputs(reveal)
    # No --limit. The frozen specification is one test over its whole eligible
    # population; a subset would be a different specification wearing the name.
    reveal.add_argument("--i-have-reviewed-the-design", action="store_true")
    reveal.add_argument("--selection-sha256", default=None)
    reveal.set_defaults(handler=run)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    revealed = args.command == "run"
    run_command(
        args.handler,
        args,
        banner=(
            f"{STAGE41_PLAN_VERSION}/{STAGE41_AMENDMENT} :: {args.command} :: "
            f"trials {EFFECTIVE_TRIALS_BEFORE} -> "
            f"{EFFECTIVE_TRIALS_AFTER_REVEAL if revealed else EFFECTIVE_TRIALS_AFTER_DESIGN}"
        ),
    )


if __name__ == "__main__":
    main()
