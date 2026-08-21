"""Stage 4.1 IAG-v2 CLI: raw-MBO diagnose, timing probe, and the gated reveal.

``plan`` and ``semantics`` need no data. ``probe`` times one symbol-day so the
full pass can be estimated before it is committed to. ``diagnose`` streams raw
MBO through ``t_obs_end`` and selects a specification from counts alone -- it
cannot compute an outcome, because it never imports the function that would.

``run`` is the Stage-4.2 reveal. It moves the ledger 531 -> 532, once, for the
specification ``diagnose`` already committed to.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.stage41_iag_v2_plan import (
    CERTIFIED_SYMBOLS,
    DIAGNOSTIC_FILENAME,
    EFFECTIVE_TRIALS_AFTER_DESIGN,
    EFFECTIVE_TRIALS_AFTER_REVEAL,
    EFFECTIVE_TRIALS_BEFORE,
    FORBIDDEN_AS_DIRECTIONAL_EVIDENCE,
    MIN_BASELINE_TILES,
    MIN_RAW_RECORD_REQUIREMENT,
    MIN_TRADE_REQUIREMENT,
    NANOS_PER_MINUTE,
    OBSERVATION_NS,
    OBSERVATION_SECONDS,
    PRIMARY_HORIZON_MINUTES,
    PROBE_FILENAME,
    QUALITY_GATES,
    QUIET_PERIOD_MINUTES,
    REPORT_RELATIVE_DIR,
    RESULTS_FILENAME,
    SECONDARY_HORIZON_MINUTES,
    SELECTION_FILENAME,
    STAGE41_V2_PLAN_VERSION,
    assert_frozen_design,
    statistical_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT_DIR = REPO_ROOT / REPORT_RELATIVE_DIR

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
    from app.services.stage41_iag_v2_executor import STAGE41_V2_EXECUTOR_VERSION

    return {
        "stage41_v2_plan_version": STAGE41_V2_PLAN_VERSION,
        "stage41_v2_executor_version": STAGE41_V2_EXECUTOR_VERSION,
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
    design = assert_frozen_design(REPO_ROOT)
    payload = {**_governance(revealed=False), "design": design, **statistical_plan()}
    _write(Path(args.output_dir) / "stage41_v2_plan.json", payload)
    return payload


def semantics(_args: argparse.Namespace) -> dict[str, Any]:
    """What each raw record means. Needs no data."""
    from app.services.stage41_iag_v2_plan import (
        ACM_SIDE_MEANS,
        DEPTH_LEVELS,
        EXECUTION_GROUP_MARKER_ACTIONS,
        FILL_IS_SIGNED,
        FILL_SIDE_MEANS,
        TRADE_SIDE_MEANS,
    )

    return {
        **_governance(revealed=False),
        "actions": {
            "A": {"book": "insert resting order (F_TOB replaces the side)",
                  "side": "resting"},
            "C": {"book": "reduce named order, clamped at 0", "side": "resting"},
            "M": {"book": "change price and/or size of named order",
                  "side": "resting"},
            "R": {"book": "clear", "side": "N"},
            "T": {"book": "neutral", "side": TRADE_SIDE_MEANS},
            "F": {"book": "neutral", "side": FILL_SIDE_MEANS},
            "N": {"book": "neutral", "side": "-"},
        },
        "signed_records": "ACTION_TRADE only",
        "fill_is_signed": FILL_IS_SIGNED,
        "why_fill_is_not_signed": (
            "a fill's side is the resting side -- the opposite of the aggressor "
            "-- and the trade already carries the quantity, so signing it would "
            "be wrong twice over: inverted, and double counted"
        ),
        "acm_side_means": ACM_SIDE_MEANS,
        "execution_group_marker_actions": list(EXECUTION_GROUP_MARKER_ACTIONS),
        "why_execution_groups_are_excluded": (
            "XNAS normalizes one execution as T -> F -> C; the C is caused by "
            "the execution, so counting it as a voluntary cancellation would "
            "count one execution twice"
        ),
        "modify_decomposition": {
            "price_changed": "withdraw old resting size",
            "same_price_size_increase": "add the difference; loses priority",
            "same_price_size_decrease": "withdraw the difference; keeps priority",
            "unknown_order": "treated as an add",
        },
        "state_selection_rule": (
            "S(t) = state at the latest coherent F_LAST with ts_recv <= t; never "
            "nearest, never after, undefined if none or one-sided"
        ),
        "depth_levels": DEPTH_LEVELS,
        "impacted_side_rule": {"+1": "ASK", "-1": "BID"},
        "quality_gates": list(QUALITY_GATES),
        "min_raw_record_requirement": MIN_RAW_RECORD_REQUIREMENT,
        "min_trade_requirement": MIN_TRADE_REQUIREMENT,
        "forbidden_as_directional_evidence": list(FORBIDDEN_AS_DIRECTIONAL_EVIDENCE),
    }


# ---------------------------------------------------------------------------
# Shared measurement
# ---------------------------------------------------------------------------


def _news_events(args: argparse.Namespace) -> list[dict[str, Any]]:
    """The Stage-4.0 population, reusing its machinery rather than re-deriving it."""
    from datetime import timedelta

    from app.cli.stage40_audit import _certified_sessions, _open_connection
    from app.db import connect
    from app.services.stage40_audit import _epoch_nanoseconds, load_l3_coverage

    features_dir = Path(args.features_dir)
    if not features_dir.is_dir():
        raise ValueError(f"the Stage-1 feature directory is missing at {features_dir}")
    coverage = load_l3_coverage(
        features_dir, symbols=CERTIFIED_SYMBOLS, sessions=_certified_sessions()
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


def _raw_path(args: argparse.Namespace, symbol: str, session_date: str) -> Path | None:
    """Resolve the certified raw file through Stage-1 provenance."""
    from app.services.mbo_stage3_executor import resolve_raw_source

    stem = f"{symbol}_{session_date}"
    manifest = Path(args.features_dir) / "manifests" / f"{stem}.manifest.json"
    if not manifest.is_file():
        return None
    try:
        return Path(
            resolve_raw_source(
                json.loads(manifest.read_text(encoding="utf-8")),
                Path(args.raw_dir),
                stem=stem,
            )
        )
    except (ValueError, FileNotFoundError):
        return None


def _scan_sessions(args: argparse.Namespace, events: list[dict[str, Any]]):
    """One replay per symbol-day, in chronological order so baselines stay causal."""
    from app.services.mbo_book_validator import iter_dbn_events
    from app.services.stage41_iag_v2_executor import (
        SessionScanner,
        build_baseline,
        measure_event,
    )

    by_symbol: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for event in events:
        by_symbol.setdefault(event["symbol"], {}).setdefault(
            event["session_date"], []
        ).append(event)

    states = []
    unresolved_files = 0
    tile_growth: list[dict[str, Any]] = []

    for symbol in sorted(by_symbol):
        tiles: list[Any] = []
        for session_date in sorted(by_symbol[symbol]):
            session_events = sorted(
                by_symbol[symbol][session_date], key=lambda e: e["t0_ns"]
            )
            path = _raw_path(args, symbol, session_date)
            if path is None or not path.is_file():
                unresolved_files += 1
                continue
            print(f"  {symbol} {session_date}", flush=True)

            scanner = SessionScanner([e["t0_ns"] for e in session_events])
            scanner.run(iter_dbn_events(str(path)))

            # The baseline holds only prior tiles: this session folds in after
            # its own events have been measured against what came before.
            baseline = build_baseline(symbol, tiles)
            for event in session_events:
                states.append(
                    measure_event(
                        symbol=symbol,
                        session_date=session_date,
                        story_id=event["story_id"],
                        t0_ns=event["t0_ns"],
                        stats=scanner.event_intervals.get(event["t0_ns"]),
                        baseline=baseline,
                        gate_failure=scanner.event_gate_failures.get(event["t0_ns"]),
                    )
                )
            tiles.extend(scanner.tiles)
            tile_growth.append(
                {"symbol": symbol, "through": session_date, "tiles": len(tiles)}
            )
    return {
        "states": states,
        "unresolved_files": unresolved_files,
        "tile_growth": tile_growth,
    }


def probe(args: argparse.Namespace) -> dict[str, Any]:
    """Time one symbol-day so the full pass can be estimated. Outcome-blind.

    Reports throughput and window counts only. It computes no state percentile,
    no direction verdict and no price after ``t_obs_end`` -- a probe that could
    report an outcome would not be a probe.
    """
    from app.services.mbo_book_validator import iter_dbn_events
    from app.services.stage41_iag_v2_executor import SessionScanner

    events = _news_events(args) if args.with_events else []
    symbol, session_date = args.symbol, args.session_date
    starts = sorted(
        e["t0_ns"]
        for e in events
        if e["symbol"] == symbol and e["session_date"] == session_date
    )

    path = _raw_path(args, symbol, session_date)
    if path is None or not path.is_file():
        raise ValueError(
            f"no certified raw file for {symbol} {session_date}; cannot probe"
        )

    scanner = SessionScanner(starts)
    started = time.perf_counter()
    scanner.run(iter_dbn_events(str(path)))
    elapsed = time.perf_counter() - started

    peak_mb = None
    try:
        import resource

        peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except (ImportError, AttributeError):
        pass

    rate = scanner.records / elapsed if elapsed > 0 else None
    payload = {
        **_governance(revealed=False),
        "probe_only": True,
        "symbol": symbol,
        "session_date": session_date,
        "raw_records": scanner.records,
        "elapsed_seconds": round(elapsed, 3),
        "records_per_second": round(rate, 1) if rate else None,
        "coherent_states": len(scanner.tiles),
        "baseline_tiles": len(scanner.tiles),
        "observation_windows_encountered": len(scanner.event_intervals),
        "peak_rss_mb": peak_mb,
        "estimated_full_pass_seconds": (
            round(scanner.records * 160 / rate, 1) if rate else None
        ),
        "estimate_basis": "this file's throughput extrapolated to 160 symbol-days",
        "contains_post_decision_return": False,
    }
    _write(Path(args.output_dir) / PROBE_FILENAME, _strip_outcomes(payload))
    return payload


def diagnose(args: argparse.Namespace) -> dict[str, Any]:
    """Every gate the reveal depends on, and no economic outcome.

    This never imports ``gross_directional_displacement_bps``, so no
    post-``t_obs_end`` price can be read from here even by mistake.
    """
    from app.services.stage41_iag_v2_executor import (
        FAILURE_REASONS,
        select_specification,
        write_selection,
    )

    design = assert_frozen_design(REPO_ROOT)
    events = _news_events(args)
    scanned = _scan_sessions(args, events)
    states = scanned["states"]

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

    payload = {
        **_governance(revealed=False),
        "design": design,
        "diagnostic_only": True,
        "candidate_events": len(events),
        "unresolved_raw_files": scanned["unresolved_files"],
        "observation_window_seconds": OBSERVATION_SECONDS,
        "raw_coverage": {
            "complete_raw_observation_events": sum(
                1 for s in states if s.gate_failure is None
            ),
            "gate_failures": _tally([s.gate_failure for s in states if s.gate_failure]),
            "raw_records_per_window": _distribution([s.records for s in states]),
            "signable_trades_per_window": _distribution(
                [s.signable_trades for s in states]
            ),
            "unsignable_trades_per_window": _distribution(
                [s.unsignable_trades for s in states]
            ),
            "coherent_states_per_window": _distribution(
                [s.coherent_states for s in states]
            ),
        },
        "direction": {
            "long": sum(1 for s in states if s.direction == 1),
            "short": sum(1 for s in states if s.direction == -1),
            "ambiguous": sum(1 for s in states if s.direction is None),
            "ambiguity_reasons": _tally(
                [s.direction_reason for s in states if s.direction_reason]
            ),
            "quarter_agreement_histogram": _tally(
                [str(s.agreeing_quarters) for s in states]
            ),
            "cadence_agreement_required": False,
        },
        "baseline": {
            "sufficient": sum(
                1 for s in states if s.baseline_tiles >= MIN_BASELINE_TILES
            ),
            "insufficient": sum(
                1 for s in states if s.baseline_tiles < MIN_BASELINE_TILES
            ),
            "min_tiles_required": MIN_BASELINE_TILES,
            "tiles_per_event": _distribution([s.baseline_tiles for s in states]),
        },
        "lambda": {
            "defined": sum(1 for s in states if s.lambda_value is not None),
            "undefined": sum(1 for s in states if s.lambda_value is None),
        },
        "state_gates": {
            "m2_pass": sum(
                1
                for s in states
                if s.depth_percentile is not None and s.depth_percentile <= 25.0
            ),
            "m3_pass": sum(
                1 for s in states if s.recovery_ratio is not None and s.recovery_ratio <= 0.25
            ),
            "absorption_pass": sum(
                1
                for s in states
                if s.absorption_percentile is None or s.absorption_percentile < 75.0
            ),
            "supporting_pass": {
                "S1_withdrawal": _at_or_above(states, "withdrawal_percentile"),
                "S2_spread": _at_or_above(states, "spread_percentile"),
                "S3_lambda": _at_or_above(states, "lambda_percentile"),
                "S4_intensity": _at_or_above(states, "intensity_percentile"),
            },
        },
        "causal_checks": {
            "all_windows_are_120s": all(
                s.t_obs_end_ns == s.t0_ns + OBSERVATION_NS for s in states
            ),
            "no_post_t_obs_end_price_access": True,
            "state_selection_rule": "latest coherent F_LAST with ts_recv <= t",
        },
        "failure_reason_vocabulary": list(FAILURE_REASONS),
        "specification_selection": selection,
        "selection_record_sha256": selection_sha,
        "contains_stage_4_2_outcome": False,
    }
    clean = _strip_outcomes(payload)
    _write(output_dir / DIAGNOSTIC_FILENAME, clean)
    return clean


def _at_or_above(states, attribute: str, threshold: float = 75.0) -> int:
    return sum(
        1
        for s in states
        if getattr(s, attribute) is not None and getattr(s, attribute) >= threshold
    )


def _distribution(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "p25": None, "median": None, "p75": None,
                "max": None}
    ordered = sorted(values)

    def at(fraction: float):
        return ordered[min(int(fraction * len(ordered)), len(ordered) - 1)]

    return {
        "count": len(ordered),
        "min": ordered[0],
        "p25": at(0.25),
        "median": at(0.50),
        "p75": at(0.75),
        "max": ordered[-1],
    }


def _tally(values: list[str]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return [{"label": k, "count": v} for k, v in sorted(counts.items())]


def run(args: argparse.Namespace) -> dict[str, Any]:
    """The Stage-4.2 economic reveal. One specification, once."""
    from app.services.mbo_book_validator import iter_dbn_events
    from app.services.stage41_iag_v2_executor import (
        SessionScanner,
        decide_verdict,
        gross_directional_displacement_bps,
        qualifies,
        read_selection,
        session_clustered_inference,
        specification_by_name,
    )

    if not args.i_have_reviewed_the_design:
        raise ValueError(
            "the Stage-4.2 economic reveal is not authorized. It exposes the one "
            "primary IAG-v2 specification and advances the ledger "
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
    scanned = _scan_sessions(args, events)
    eligible = [s for s in scanned["states"] if qualifies(s, spec)[0]]

    all_horizons = (PRIMARY_HORIZON_MINUTES, *SECONDARY_HORIZON_MINUTES)
    horizons: dict[int, list[float]] = {m: [] for m in all_horizons}
    displacements: list[float] = []
    sessions: list[str] = []
    per_symbol: dict[str, list[float]] = {}
    per_session: dict[str, list[float]] = {}
    unresolved = 0

    by_session: dict[tuple[str, str], list[Any]] = {}
    for state in eligible:
        by_session.setdefault((state.symbol, state.session_date), []).append(state)

    for (symbol, session_date), group in sorted(by_session.items()):
        path = _raw_path(args, symbol, session_date)
        if path is None or not path.is_file():
            unresolved += len(group)
            continue
        # A horizon scan is a second, separate pass whose only purpose is to
        # read midpoints after t_obs_end. It exists nowhere but here.
        instants = sorted(
            {s.t_obs_end_ns for s in group}
            | {
                s.t_obs_end_ns + m * NANOS_PER_MINUTE
                for s in group
                for m in all_horizons
            }
        )
        scanner = SessionScanner([])
        midpoints = _midpoints_at(scanner, iter_dbn_events(str(path)), instants)

        for state in group:
            decision = midpoints.get(state.t_obs_end_ns)
            if decision is None:
                unresolved += 1
                continue
            computed: dict[int, float] = {}
            for minutes in all_horizons:
                horizon_mid = midpoints.get(
                    state.t_obs_end_ns + minutes * NANOS_PER_MINUTE
                )
                if horizon_mid is None:
                    break
                computed[minutes] = gross_directional_displacement_bps(
                    direction=state.direction,
                    midpoint_at_decision=decision,
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
            f"{m}min": _summary(v) for m, v in horizons.items()
            if m != PRIMARY_HORIZON_MINUTES
        },
        "secondary_may_rescue_primary": False,
        "per_symbol_diagnostic": {k: _summary(v) for k, v in sorted(per_symbol.items())},
        "per_session_diagnostic": {
            k: _summary(v) for k, v in sorted(per_session.items())
        },
        "diagnostics_may_filter_population": False,
        "is_pnl": False,
        "is_executable_profit": False,
    }
    _write(results_path, report)
    return {k: v for k, v in report.items() if k != "per_session_diagnostic"}


def _midpoints_at(scanner, events, instants: list[int]) -> dict[int, float]:
    """Latest coherent midpoint at or before each requested instant.

    The same state-selection rule the qualification path uses, applied to
    instants that happen to lie after ``t_obs_end``. Reachable only from ``run``.
    """
    from app.services.mbo_book_validator import F_LAST
    from app.services.stage41_iag_v2_executor import capture_state

    wanted = sorted(instants)
    resolved: dict[int, float] = {}
    index = 0
    latest = None
    for event in events:
        ts = int(event.ts_recv)
        while index < len(wanted) and ts > wanted[index]:
            if latest is not None and latest.midpoint is not None:
                resolved[wanted[index]] = latest.midpoint
            index += 1
        scanner.book.apply(event)
        if event.flags & F_LAST:
            latest = capture_state(scanner.book, ts)
    while index < len(wanted):
        if latest is not None and latest.midpoint is not None:
            resolved[wanted[index]] = latest.midpoint
        index += 1
    return resolved


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
        prog="keftrade-stage41-iag-v2",
        description=(
            "Stage 4.1 IAG-v2 raw MBO: outcome-blind diagnose, timing probe, and "
            "the single gated Stage-4.2 reveal."
        ),
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_cmd = subparsers.add_parser("plan", help="Emit the frozen IAG-v2 plan.")
    plan_cmd.set_defaults(handler=plan)

    sem_cmd = subparsers.add_parser(
        "semantics", help="Exact raw-record semantics; needs no data."
    )
    sem_cmd.set_defaults(handler=semantics)

    def add_inputs(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--features-dir", required=True)
        cmd.add_argument("--raw-dir", required=True)

    probe_cmd = subparsers.add_parser(
        "probe", help="Time one symbol-day. Outcome-blind; reports no price."
    )
    add_inputs(probe_cmd)
    probe_cmd.add_argument("--symbol", required=True)
    probe_cmd.add_argument("--session-date", required=True)
    probe_cmd.add_argument("--with-events", action="store_true")
    probe_cmd.set_defaults(handler=probe)

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
            f"{STAGE41_V2_PLAN_VERSION} :: {args.command} :: trials "
            f"{EFFECTIVE_TRIALS_BEFORE} -> "
            f"{EFFECTIVE_TRIALS_AFTER_REVEAL if revealed else EFFECTIVE_TRIALS_AFTER_DESIGN}"
        ),
    )


if __name__ == "__main__":
    main()
