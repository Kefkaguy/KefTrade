from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.settings import settings
from app.services.edge_discovery import run_edge_discovery


REPORT_DIR = Path(__file__).resolve().parent


def write_reports(payload: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "evidence.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    (REPORT_DIR / "README.md").write_text(readme(payload), encoding="utf-8")
    (REPORT_DIR / "source_backed_discovery_report.md").write_text(discovery_report(payload), encoding="utf-8")
    (REPORT_DIR / "lineage_and_contradictions.md").write_text(lineage_report(payload), encoding="utf-8")
    (REPORT_DIR / "regression_and_compute.md").write_text(regression_report(payload), encoding="utf-8")


def readme(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Phase 3 Edge Discovery Engine",
            "",
            "Phase 3 converts preserved successful and failed research outcomes into new standard KefTrade hypothesis versions.",
            "",
            "No Phase 4 work was started. No VPS, broker, paper-routing, live-routing, UI page, validation threshold, or candidate-volume expansion was added.",
            "",
            "## Stored output",
            "",
            f"- Hypotheses stored: `{len(payload.get('stored_hypotheses') or [])}`",
            f"- Table: `{(payload.get('storage') or {}).get('table')}`",
            f"- Format: `{(payload.get('storage') or {}).get('format')}`",
            f"- Dataset: `{payload.get('dataset_id')}`",
            f"- Campaigns analyzed: `{', '.join(str(item) for item in payload.get('campaign_ids_analyzed') or [])}`",
            "",
            "Every generated discovery is post-hoc and unconfirmed until independently tested on a future frozen dataset.",
        ]
    )


def discovery_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Source-Backed Discovery Report",
        "",
        f"Engine version: `{payload.get('edge_discovery_version')}`",
        f"Jobs analyzed: `{payload.get('jobs_analyzed')}`",
        f"Unique executable keys: `{payload.get('unique_execution_keys')}`",
        "",
        "## Generated Hypotheses",
        "",
    ]
    for row in payload.get("stored_hypotheses") or []:
        summary = row.get("test_summary") or {}
        window = row.get("evidence_window") or {}
        lines.extend(
            [
                f"### Hypothesis `{row.get('id')}` - {row.get('title')}",
                "",
                f"- Status: `{row.get('status')}`",
                f"- Strategy family: `{row.get('strategy_family')}`",
                f"- Scope: `{row.get('scope_type')}:{row.get('scope_ref')}`",
                f"- Discovery type: `{summary.get('discovery_type')}`",
                f"- Source dataset: `{summary.get('source_dataset_id')}`",
                f"- Campaign IDs: `{', '.join(str(item) for item in summary.get('campaign_ids') or [])}`",
                f"- Sample size: `{window.get('sample_size')}` jobs, `{window.get('unique_execution_keys')}` executable keys",
                f"- Confidence score: `{row.get('confidence_score')}`",
                f"- Label: `Post-hoc and unconfirmed.`",
                "",
                row.get("hypothesis") or "",
                "",
                f"Supporting evidence refs: `{len(row.get('supporting_evidence') or [])}`. Contradictory evidence refs: `{len(row.get('contradictory_evidence') or [])}`.",
                "",
            ]
        )
    if not payload.get("stored_hypotheses"):
        lines.append("Inconclusive - insufficient evidence.")
    return "\n".join(lines)


def lineage_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Evidence Lineage And Contradictions",
        "",
        "All generated hypotheses store supporting and contradictory evidence arrays on `research_hypothesis_versions`.",
        "",
    ]
    for row in payload.get("stored_hypotheses") or []:
        lines.extend(
            [
                f"## Hypothesis `{row.get('id')}`",
                "",
                f"- Supporting refs: `{', '.join((row.get('supporting_evidence') or [])[:8])}`",
                f"- Contradictory refs: `{', '.join((row.get('contradictory_evidence') or [])[:8])}`",
                f"- Candidate IDs: `{', '.join((row.get('evidence_window') or {}).get('candidate_ids') or [])}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Lifecycle Interpretation",
            "",
            "Historical hypothesis records were not edited. Inconsistent confirmed wording is represented by derived lifecycle interpretation records.",
            "",
        ]
    )
    for row in payload.get("lifecycle_interpretations") or []:
        if row.get("wording_status_inconsistent"):
            lines.append(
                f"- Hypothesis `{row.get('hypothesis_id')}`: stored status `{row.get('stored_status')}`, authoritative confirmation `{row.get('authoritative_confirmation_status')}`."
            )
    return "\n".join(lines)


def regression_report(payload: dict[str, Any]) -> str:
    controls = payload.get("controls") or {}
    lines = [
        "# Regression And Compute",
        "",
        "Phase 3 did not run a broad candidate campaign. It performs a bounded read of preserved evidence and appends standard hypothesis versions.",
        "",
        f"- Validation thresholds changed: `{controls.get('validation_thresholds_changed')}`",
        f"- Candidate volume increased: `{controls.get('candidate_volume_increased')}`",
        f"- Minimum family jobs: `{controls.get('minimum_family_jobs')}`",
        f"- Minimum computed results: `{controls.get('minimum_computed_results')}`",
        f"- Minimum executable keys: `{controls.get('minimum_unique_executions')}`",
        f"- Multiple-comparison handling: `{controls.get('multiple_comparison_awareness')}`",
        "",
        "Generator-consumption regression is covered by `apps/api/tests/test_edge_discovery.py`.",
        "",
        "No post-hoc hypothesis is presented as confirmed from the same evidence used to create it.",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 3 Edge Discovery against preserved research evidence.")
    parser.add_argument("--dataset-id", type=int, default=None)
    parser.add_argument("--max-hypotheses", type=int, default=12)
    parser.add_argument("--write-reports", action="store_true")
    args = parser.parse_args()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        payload = run_edge_discovery(conn, dataset_id=args.dataset_id, max_hypotheses=args.max_hypotheses)
    if args.write_reports:
        write_reports(payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
