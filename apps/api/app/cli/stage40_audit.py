"""Stage 4.0 CLI: the information-assimilation feasibility audit.

Four commands. Three of them need no database and can be checked anywhere; only
``audit`` reads production data, and even that computes no return.

There is no ``run`` here and no gating flag, because there is nothing to gate.
Stage 3.6 needed ``--i-have-reviewed-the-design`` because running it spent a
trial. This stage spends none: the ledger reads 531 before and 531 after.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from app.cli._refusal import run_command
from app.services.stage40_audit import (
    AUDITED_TABLES,
    STAGE40_AUDIT_VERSION,
    build_manifest,
    cross_source_overlap,
    event_supply_adequacy,
    inventory_columns,
    mbo_state_feature_feasibility,
    measure_field_certifications,
    measure_news_event_supply,
    measure_option_quality,
    measure_table_coverage,
    options_feasibility,
    put_call_parity_feasible,
    recommend,
    stock_flow_feasibility,
    write_report,
)
from app.services.stage40_audit_plan import (
    AUDIT_WINDOWS,
    CERTIFIED_L3_WINDOW,
    EFFECTIVE_TRIALS_AFTER,
    EFFECTIVE_TRIALS_BEFORE,
    MISNAMED_OPTION_FEATURES,
    OPTION_FIELD_SEMANTICS,
    OPTIONS_FORWARD_WINDOW,
    OUTCOME_BEARING_TOKENS,
    OUTCOME_TOKEN_EXEMPTIONS,
    REPORT_RELATIVE_DIR,
    STAGE40_PLAN_VERSION,
    TIMESTAMP_REGISTRY,
    statistical_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT_DIR = REPO_ROOT / REPORT_RELATIVE_DIR

# The quiet period Stage 3.6 declared. Reused rather than re-chosen: picking a
# new isolation rule here would make the two event populations incomparable for
# no reason other than that this stage came later.
DEFAULT_QUIET_MINUTES = 60


def _strip_outcomes(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove anything that could be an economic outcome, by key name.

    Keys only, never values. The audit's prose legitimately explains *why* a
    forward return is forbidden, and a filter that ate its own explanation would
    make the report unreadable while proving nothing -- the real defence is that
    no code here computes such a quantity, which the tests assert directly.
    """
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
    write_report(payload, path)


def _governance() -> dict[str, Any]:
    """The block every artifact carries."""
    return {
        "stage40_plan_version": STAGE40_PLAN_VERSION,
        "stage40_audit_version": STAGE40_AUDIT_VERSION,
        "contains_strategy_outcome": False,
        "contains_post_decision_return": False,
        "contains_pnl": False,
        "effective_trials_before": EFFECTIVE_TRIALS_BEFORE,
        "effective_trials_after": EFFECTIVE_TRIALS_AFTER,
        "authorizes_paper_or_live": False,
    }


def plan(args: argparse.Namespace) -> dict[str, Any]:
    """The declared plan, before anything is measured."""
    payload = {**_governance(), **statistical_plan()}
    _write(Path(args.output_dir) / "stage40_plan.json", payload)
    return payload


def timestamps(_args: argparse.Namespace) -> dict[str, Any]:
    """Every clock in the system, with what it means. Needs no database."""
    entries = [
        {
            "table": entry.table,
            "column": entry.column,
            "kind": entry.kind,
            "resolution_ns": entry.resolution_ns,
            "timezone": entry.timezone,
            "decision_safe": entry.decision_safe,
            "note": entry.note,
        }
        for entry in TIMESTAMP_REGISTRY
    ]
    unsafe = [e for e in entries if not e["decision_safe"]]
    return {
        **_governance(),
        "clocks_declared": len(entries),
        "clocks_safe_at_decision_time": len(entries) - len(unsafe),
        "clocks_refused_at_decision_time": [
            {"table": e["table"], "column": e["column"], "why": e["note"]} for e in unsafe
        ],
        "coarsest_safe_resolution_ns": max(
            e["resolution_ns"] for e in entries if e["decision_safe"]
        ),
        "registry": entries,
    }


def semantics(_args: argparse.Namespace) -> dict[str, Any]:
    """What the stored option fields actually contain. Needs no database."""
    from app.cli.mbo_stage2 import FEATURE_NAMES

    fields = [
        {
            "column": field.column,
            "present": field.present,
            "interpretation": field.interpretation,
            "usable_as_flow": field.usable_as_flow,
        }
        for field in OPTION_FIELD_SEMANTICS
    ]
    return {
        **_governance(),
        "option_fields": fields,
        "option_fields_absent": [f["column"] for f in fields if not f["present"]],
        "misnamed_features_not_to_use": list(MISNAMED_OPTION_FEATURES),
        "misnaming_note": (
            "option_call_volume and option_put_volume sum trade_size across "
            "contracts. trade_size is the size of the single most recent trade, "
            "so these are sums of last-trade sizes and do not accumulate. They "
            "must not be read as volume or as flow."
        ),
        "put_call_parity": put_call_parity_feasible(),
        "l3_state_feasibility": mbo_state_feature_feasibility(FEATURE_NAMES),
    }


def audit(args: argparse.Namespace) -> dict[str, Any]:
    """The full audit. Reads production data; computes no return.

    Every window is measured independently and reported independently. The two
    richest sources live in different years, and collapsing them into one
    verdict would describe a dataset we do not have.
    """
    from app.db import connect

    output_dir = Path(args.output_dir)
    quiet_minutes = int(args.quiet_minutes)
    tables = [name for name, _clock, _symbol in AUDITED_TABLES]

    coverages = []
    supplies = []
    per_window: dict[str, Any] = {}

    with _open_connection(connect) as conn, conn.cursor() as cursor:
        catalogue = inventory_columns(cursor, tables)
        missing = sorted(k for k, v in catalogue.items() if not v["present"])
        if missing:
            raise ValueError(
                f"these audited tables do not exist in this database: "
                f"{missing}. Stage 4.0 will not report coverage for a table "
                "it cannot see."
            )
        certifications = measure_field_certifications(cursor)

        for window in AUDIT_WINDOWS:
            window_coverages = [
                measure_table_coverage(
                    cursor,
                    table=table,
                    clock=clock,
                    symbol_column=symbol,
                    window=window,
                )
                for table, clock, symbol in AUDITED_TABLES
            ]
            coverages.extend(window_coverages)

            option_coverage = next(
                (
                    c
                    for c in window_coverages
                    if c.table == "intraday_option_chain_snapshots"
                ),
                None,
            )
            quality = (
                measure_option_quality(cursor, window=window)
                if option_coverage and not option_coverage.is_empty
                else None
            )

            l3_sessions = (
                _certified_sessions() if window is CERTIFIED_L3_WINDOW else []
            )
            option_days = _days_between(
                option_coverage.first_instant, option_coverage.last_instant
            ) if option_coverage and not option_coverage.is_empty else []

            supply = measure_news_event_supply(
                cursor,
                window=window,
                quiet_minutes=quiet_minutes,
                l3_sessions=l3_sessions,
                option_days=option_days,
            )
            supplies.append(supply)
            per_window[window.name] = {
                "coverage": [c.as_dict() for c in window_coverages],
                "option_quality": quality.as_dict() if quality else None,
                "event_supply": supply.as_dict(),
                "event_supply_adequacy": event_supply_adequacy(supply),
                "options_feasibility": options_feasibility(
                    coverage=option_coverage,
                    quality=quality,
                    overlaps_l3=bool(l3_sessions),
                ),
            }

    from app.cli.mbo_stage2 import FEATURE_NAMES

    l3_state = mbo_state_feature_feasibility(FEATURE_NAMES)
    overlap = cross_source_overlap(coverages)
    flow = stock_flow_feasibility(
        trade_flow_coverage=next(
            (c for c in coverages if c.table == "intraday_trade_flow_features"), None
        ),
        quote_coverage=next(
            (c for c in coverages if c.table == "intraday_quote_snapshots"), None
        ),
        decertified_fields=[
            f"{row['table_name']}.{row['field_name']}" for row in certifications
        ],
    )
    # The certified window drives the recommendation: it is the only one where
    # book state exists, and a mechanism is recommended on the data that can
    # actually express it.
    best_options = per_window[CERTIFIED_L3_WINDOW.name]["options_feasibility"]
    if best_options["verdict"] == "options_data_not_suitable":
        forward = per_window.get(OPTIONS_FORWARD_WINDOW.name, {})
        best_options = forward.get("options_feasibility", best_options)

    adequacy = [entry["event_supply_adequacy"] for entry in per_window.values()]
    recommendation = recommend(
        l3_state=l3_state,
        options=best_options,
        stock_flow=flow,
        supply=adequacy,
        overlap=overlap,
    )

    report = {
        **_governance(),
        "quiet_period_minutes": quiet_minutes,
        "data_inventory": catalogue,
        "field_certifications_honoured": certifications,
        "timestamp_audit": timestamps(args),
        "news_feasibility": _news_feasibility(supplies),
        "options_feasibility": {
            "per_window": {k: v["options_feasibility"] for k, v in per_window.items()},
            "binding_verdict": best_options["verdict"],
            "put_call_parity": put_call_parity_feasible(),
        },
        "stock_flow_feasibility": flow,
        "mbo_state_feature_feasibility": l3_state,
        "cross_source_overlap": overlap,
        "event_supply": {k: v["event_supply"] for k, v in per_window.items()},
        "event_supply_adequacy": adequacy,
        "missing_information": _missing_information(flow, best_options, l3_state),
        "recommended_next_mechanism": recommendation,
        "per_window": per_window,
    }

    clean = _strip_outcomes(report)
    _write(output_dir / "stage40_audit_report.json", clean)
    _write(output_dir / "stage40_plan.json", {**_governance(), **statistical_plan()})
    _write(output_dir / "stage40_timestamp_audit.json", timestamps(args))
    _write(output_dir / "stage40_semantics.json", semantics(args))

    manifest = build_manifest(
        output_dir,
        artifacts=(
            "stage40_audit_report.json",
            "stage40_plan.json",
            "stage40_timestamp_audit.json",
            "stage40_semantics.json",
        ),
    )
    _write(output_dir / "stage40_audit_manifest.json", manifest)

    summary = {k: v for k, v in clean.items() if k not in ("per_window", "data_inventory")}
    summary["manifest"] = manifest
    return summary



def _open_connection(connect):
    """Open the database, or refuse by name rather than by traceback.

    A detached container writing to a log file is exactly where an unreadable
    stack trace costs the most: it makes an unreachable database look like a
    defect in the audit. Same treatment the Stage-3.5/3.6 CLIs give it.
    """
    try:
        return connect()
    except Exception as error:  # psycopg raises several connection types
        raise ValueError(
            "Stage 4.0 cannot reach the database, so it cannot measure "
            f"coverage or event supply: {type(error).__name__}: {error}. The "
            "audit reads production data and must run where that data lives."
        ) from error


def _news_feasibility(supplies: list[Any]) -> dict[str, Any]:
    """What the news channel can and cannot support, from measured supply."""
    return {
        "event_clock": "intraday_news_articles.known_at",
        "event_clock_basis": "max(created_at, updated_at)",
        "event_clock_resolution_ns": 1_000_000_000,
        "receive_clock_refused": "intraday_news_articles.received_at",
        "receive_clock_refusal_reason": (
            "DEFAULT NOW() at ingest, so 2025 stories carry 2026 receive times"
        ),
        "category_field_present": False,
        "materiality_field_present": False,
        "scheduled_vs_unscheduled_recoverable": "unknown_requires_raw_payload_inspection",
        "duplicate_identity": "COALESCE(content_hash, article_id)",
        "per_window_supply": [supply.as_dict() for supply in supplies],
    }


def _missing_information(
    flow: dict[str, Any], options: dict[str, Any], l3_state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Everything the audit found we do not have, in one place."""
    gaps = [
        {
            "gap": item["data"],
            "blocks": "market-wide signed stock flow",
            "why": item["why"],
        }
        for item in flow["would_require"]
    ]
    gaps.extend(
        {
            "gap": name,
            "blocks": "signed option flow",
            "why": "required to sign an option trade causally",
        }
        for name in options["blocking_for_signed_flow"]
    )
    gaps.extend(
        {"gap": name, "blocks": "L3 state completeness", "why": reason}
        for name, reason in l3_state["gaps"].items()
    )
    return gaps


def _certified_sessions() -> list[str]:
    """The 20 certified L3 sessions, read from the frozen Stage-3.6 census.

    Read rather than hard-coded: the census is the artifact that defines which
    sessions were certified, and a second list here could drift from it.
    """
    import csv

    path = (
        REPO_ROOT
        / "reports"
        / "tier1_stage36_preoutcome"
        / "v1"
        / "stage36_l3_consensus_preoutcome.csv"
    )
    if not path.is_file():
        raise ValueError(
            f"the frozen Stage-3.6 census is missing at {path}; Stage 4.0 reads "
            "it to learn which sessions hold certified L3 coverage"
        )
    with path.open(encoding="utf-8", newline="") as handle:
        return sorted({row["session_date"] for row in csv.DictReader(handle)})


def _days_between(first: str | None, last: str | None) -> list[str]:
    """Every calendar day spanned by an observed range."""
    from datetime import datetime, timedelta

    if not first or not last:
        return []
    start = datetime.fromisoformat(first).date()
    end = datetime.fromisoformat(last).date()
    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-stage40-audit",
        description=(
            "Stage 4.0: outcome-blind information-assimilation feasibility "
            "audit. Tests no economic specification and consumes no trial."
        ),
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_cmd = subparsers.add_parser("plan", help="Emit the declared Stage-4.0 plan.")
    plan_cmd.set_defaults(handler=plan)

    ts_cmd = subparsers.add_parser(
        "timestamps", help="Every clock and its semantics; needs no database."
    )
    ts_cmd.set_defaults(handler=timestamps)

    sem_cmd = subparsers.add_parser(
        "semantics", help="Option field and L3 state semantics; needs no database."
    )
    sem_cmd.set_defaults(handler=semantics)

    audit_cmd = subparsers.add_parser(
        "audit", help="The full audit against the database."
    )
    audit_cmd.add_argument("--quiet-minutes", default=DEFAULT_QUIET_MINUTES, type=int)
    audit_cmd.set_defaults(handler=audit)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    run_command(
        args.handler,
        args,
        banner=(
            f"{STAGE40_AUDIT_VERSION} :: {args.command} :: outcome-blind :: "
            f"trials {EFFECTIVE_TRIALS_BEFORE} -> {EFFECTIVE_TRIALS_AFTER}"
        ),
    )


if __name__ == "__main__":
    main()
