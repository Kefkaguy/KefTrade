from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.settings import settings
from app.services.deeper_observations import (
    OBSERVATION_DEFINITIONS,
    PHASE_5_OBSERVATION_VERSION,
    create_deeper_observation_hypotheses,
)


REPORT_DIR = Path(__file__).resolve().parent


def write_reports(payload: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "evidence.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    (REPORT_DIR / "README.md").write_text(readme(payload), encoding="utf-8")
    (REPORT_DIR / "observation_definitions.md").write_text(observation_definitions(payload), encoding="utf-8")
    (REPORT_DIR / "stored_hypotheses.md").write_text(stored_hypotheses(payload), encoding="utf-8")
    (REPORT_DIR / "leakage_and_reproducibility.md").write_text(leakage_and_reproducibility(payload), encoding="utf-8")
    (REPORT_DIR / "contradictory_evidence.md").write_text(contradictory_evidence(payload), encoding="utf-8")


def readme(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Phase 5 Deeper Observations",
            "",
            f"Dataset `{payload['dataset_id']}` produced `{len(payload['hypotheses'])}` standard hypothesis versions.",
            "",
            "The same deterministic measurements are also wired into the existing asset-profile observation layer for future frozen datasets.",
            "",
            "Every hypothesis is post-hoc and unconfirmed. No validation thresholds were changed, and Phase 6 was not started.",
            "",
            f"Stored hypothesis IDs: `{payload['stored_hypothesis_ids']}`.",
        ]
    )


def observation_definitions(payload: dict[str, Any]) -> str:
    lines = ["# Observation Definitions", ""]
    observed = payload["observations"]["observations"]
    for definition in OBSERVATION_DEFINITIONS:
        row = observed[definition.key]
        lines.extend(
            [
                f"## {definition.label}",
                "",
                f"- Key: `{definition.key}`",
                f"- Strategy family: `{definition.strategy_family}`",
                f"- Definition: {definition.definition}",
                f"- Expected range: `{definition.expected_range}`",
                f"- Dataset score: `{row['score']}`",
                f"- Event rate: `{row['event_rate']}`",
                f"- Sample size: `{row['sample_size']}`",
                "",
            ]
        )
    return "\n".join(lines)


def stored_hypotheses(payload: dict[str, Any]) -> str:
    lines = ["# Stored Hypotheses", ""]
    lines.append("| ID | Observation | Family | Status | Confirmation | Generator contract |")
    lines.append("| ---: | --- | --- | --- | --- | --- |")
    for row in payload["hypotheses"]:
        summary = row.get("test_summary") or {}
        lines.append(
            f"| `{row['id']}` | `{summary.get('observation_key')}` | `{row['strategy_family']}` | `{row['status']}` | `{summary.get('confirmation_status')}` | `{summary.get('candidate_generation_contract')}` |"
        )
    return "\n".join(lines)


def leakage_and_reproducibility(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Leakage And Reproducibility",
            "",
            f"- Calculation version: `{PHASE_5_OBSERVATION_VERSION}`",
            "- Future bars used: `False`",
            "- Decision point: each observation uses only candles at or before its timestamp.",
            "- Minimum rolling history: `60` bars.",
            "- Regression coverage includes a future-candle mutation test that verifies earlier observations do not change.",
            "- Existing validation policy remains `strong_research_gates:v1`.",
        ]
    )


def contradictory_evidence(payload: dict[str, Any]) -> str:
    lines = ["# Contradictory Evidence", ""]
    found = False
    for row in payload["hypotheses"]:
        contradictions = row.get("contradictory_evidence") or []
        notes = (row.get("test_summary") or {}).get("contradictory_observations") or []
        if contradictions or notes:
            found = True
            lines.extend(
                [
                    f"## Hypothesis `{row['id']}`",
                    "",
                    f"- Observation: `{(row.get('test_summary') or {}).get('observation_key')}`",
                    f"- Contradictory evidence refs: `{contradictions}`",
                    f"- Notes: `{notes}`",
                    "",
                ]
            )
    if not found:
        lines.append("No sparse-or-weak observation contradiction was triggered by the Phase 5 scoring rule, but all hypotheses remain post-hoc and unconfirmed.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write Phase 5 evidence reports.")
    parser.add_argument("--dataset-id", type=int, default=1)
    parser.add_argument("--write-reports", action="store_true")
    args = parser.parse_args()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        payload = create_deeper_observation_hypotheses(conn, dataset_id=args.dataset_id)
    if args.write_reports:
        write_reports(payload)
    print(
        json.dumps(
            {
                "dataset_id": payload["dataset_id"],
                "stored_hypothesis_ids": payload["stored_hypothesis_ids"],
                "hypothesis_count": len(payload["hypotheses"]),
                "phase6_started": payload["phase6_started"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
