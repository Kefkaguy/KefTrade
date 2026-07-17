from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.settings import settings
from app.services.multi_generation_evolution import analyze_evolution_campaign


REPORT_DIR = Path(__file__).resolve().parent


def write_reports(payload: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "evidence.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    (REPORT_DIR / "README.md").write_text(readme(payload), encoding="utf-8")
    (REPORT_DIR / "parent_eligibility.md").write_text(parent_eligibility(payload), encoding="utf-8")
    (REPORT_DIR / "lineage_and_mutations.md").write_text(lineage_and_mutations(payload), encoding="utf-8")
    (REPORT_DIR / "diversity_report.md").write_text(diversity_report(payload), encoding="utf-8")
    (REPORT_DIR / "parent_child_comparison.md").write_text(parent_child_comparison(payload), encoding="utf-8")
    (REPORT_DIR / "validation_and_compute.md").write_text(validation_and_compute(payload), encoding="utf-8")


def readme(payload: dict[str, Any]) -> str:
    campaign = payload["campaign"]
    compute = payload["compute"]
    return "\n".join(
        [
            "# Phase 4 Multi-Generation Evolution",
            "",
            f"Campaign `{campaign['id']}` completed as a bounded development run.",
            "",
            "Phase 4 created controlled descendants from promoted asset specialists only. It did not use near-pass candidates as parents, did not weaken validation thresholds, did not add infrastructure, and did not begin Phase 5.",
            "",
            "No independent frozen validation dataset exists in the local database. Therefore every descendant remains:",
            "",
            "```text",
            "Promising descendant - unconfirmed",
            "```",
            "",
            f"Jobs: `{compute['jobs']}`. Promoted jobs: `{compute['status_counts'].get('promoted', 0)}`. Rejected jobs: `{compute['status_counts'].get('rejected', 0)}`. Operational failures: `{compute['operational_failures']}`.",
            f"Confirmed improvements: `{len(payload.get('confirmed_improvements') or [])}`.",
        ]
    )


def parent_eligibility(payload: dict[str, Any]) -> str:
    phase4 = ((payload["campaign"].get("immutable_config") or {}).get("phase4") or {})
    lines = ["# Parent Eligibility", ""]
    lines.append("Only promoted asset specialists with dataset lineage, hypothesis lineage, candidate payloads, walk-forward evidence, paper readiness, and unchanged gate passes were eligible.")
    lines.append("")
    eligibility_by_id = {row["candidate_id"]: row for row in payload.get("parent_eligibility") or []}
    for parent_id in phase4.get("parent_candidate_ids") or []:
        row = eligibility_by_id.get(parent_id) or {}
        assessment = row.get("assessment") or {}
        metrics = assessment.get("metrics") or {}
        lines.append(f"## Parent `{parent_id}`")
        lines.append("")
        lines.append(f"- Source campaign: `{row.get('campaign_id')}`")
        lines.append(f"- Asset / timeframe: `{row.get('symbol')}` / `{row.get('timeframe')}`")
        lines.append(f"- Dataset ID: `{row.get('dataset_id')}`")
        lines.append(f"- Hypothesis version ID: `{row.get('hypothesis_version_id')}`")
        lines.append(f"- Strategy family: `{row.get('strategy_family')}`")
        lines.append(f"- Eligible: `{assessment.get('eligible')}`")
        lines.append(f"- Profit factor: `{metrics.get('profit_factor')}`")
        lines.append(f"- Expectancy per trade: `{metrics.get('expectancy_per_trade')}`")
        lines.append(f"- Max drawdown: `{metrics.get('max_drawdown')}`")
        lines.append(f"- Trades: `{metrics.get('number_of_trades')}`")
        lines.append("")
        lines.append("Eligibility checks:")
        lines.append("")
        for check, passed in (assessment.get("checks") or {}).items():
            lines.append(f"- `{check}`: `{passed}`")
        lines.append("")
    lines.append("")
    lines.append("Campaign `72` is preserved as contradictory pilot evidence because its first parent-selection implementation produced a diversity collapse. Campaign `73` is the corrected Phase 4 run.")
    return "\n".join(lines)


def lineage_and_mutations(payload: dict[str, Any]) -> str:
    lines = ["# Lineage And Mutations", ""]
    for row in payload.get("lineage") or []:
        mutation = row.get("mutation") or {}
        lines.extend(
            [
                f"## Child `{row.get('candidate_id')}`",
                "",
                f"- Parent: `{row.get('parent_candidate_id')}`",
                f"- Root ancestor: `{mutation.get('root_ancestor_id')}`",
                f"- Generation: `{mutation.get('generation')}`",
                f"- Mutated parameter: `{mutation.get('mutated_parameter')}`",
                f"- Old value: `{mutation.get('old_value')}`",
                f"- New value: `{mutation.get('new_value')}`",
                f"- Hypothesis ID: `{mutation.get('hypothesis_id')}`",
                f"- Dataset ID: `{mutation.get('dataset_id')}`",
                f"- Classification: `{mutation.get('classification')}`",
                "",
            ]
        )
    return "\n".join(lines)


def diversity_report(payload: dict[str, Any]) -> str:
    diversity = payload["diversity"]
    return "\n".join(
        [
            "# Diversity Report",
            "",
            f"- Children: `{diversity['children']}`",
            f"- Unique execution keys: `{diversity['unique_execution_keys']}`",
            f"- Duplicate execution keys: `{diversity['duplicate_execution_keys']}`",
            f"- Parent concentration: `{diversity['parent_concentration']}`",
            f"- Lineage entropy: `{diversity['lineage_entropy']}`",
            f"- Parameter entropy: `{diversity['parameter_entropy']}`",
            f"- Diversity collapsed: `{diversity['diversity_collapsed']}`",
            f"- Family mix: `{diversity['family_mix']}`",
            f"- Mutation parameter mix: `{diversity['mutation_parameter_mix']}`",
        ]
    )


def parent_child_comparison(payload: dict[str, Any]) -> str:
    lines = ["# Parent Versus Child Comparison", ""]
    lines.append("These comparisons are same-dataset development evidence only. They cannot confirm improvement.")
    lines.append("")
    lines.append("| Child | Parent | Status | PF delta | Expectancy delta | DD delta | Trade delta | Classification |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for row in payload.get("parent_child_comparison") or []:
        lines.append(
            f"| `{row.get('child_candidate_id')}` | `{row.get('parent_candidate_id')}` | `{row.get('status')}` | `{row.get('profit_factor_delta')}` | `{row.get('expectancy_delta')}` | `{row.get('drawdown_delta')}` | `{row.get('trade_count_delta')}` | `{row.get('classification')}` |"
        )
    return "\n".join(lines)


def validation_and_compute(payload: dict[str, Any]) -> str:
    compute = payload["compute"]
    return "\n".join(
        [
            "# Validation And Compute",
            "",
            f"- Independent validation available: `{payload['independent_validation_available']}`",
            f"- Confirmed improvements: `{len(payload.get('confirmed_improvements') or [])}`",
            f"- Jobs: `{compute['jobs']}`",
            f"- Status counts: `{compute['status_counts']}`",
            f"- Runtime ms: `{compute['runtime_ms']}`",
            f"- Median runtime ms: `{compute['median_runtime_ms']}`",
            f"- Operational failures: `{compute['operational_failures']}`",
            "",
            "Validation thresholds were unchanged (`strong_research_gates:v1`). No child is promoted as an independently confirmed improvement.",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Write Phase 4 evidence reports.")
    parser.add_argument("--campaign-id", type=int, required=True)
    parser.add_argument("--write-reports", action="store_true")
    args = parser.parse_args()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        payload = analyze_evolution_campaign(conn, args.campaign_id)
    if args.write_reports:
        write_reports(payload)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
