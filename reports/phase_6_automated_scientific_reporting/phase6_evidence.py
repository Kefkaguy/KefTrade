from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.settings import settings
from app.services.automated_scientific_reporting import PHASE_6_REPORT_VERSION, generate_automated_scientific_report


REPORT_DIR = Path(__file__).resolve().parent


def collect_evidence(conn: psycopg.Connection) -> dict[str, Any]:
    campaigns = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, name, status, dataset_id, promoted_candidates, rejected_candidates, completed_at, created_at
            FROM research_campaigns
            WHERE status = 'completed'
            ORDER BY id
            """
        ).fetchall()
    ]
    reports = []
    errors = []
    for campaign in campaigns:
        try:
            report = generate_automated_scientific_report(conn, int(campaign["id"]))
            reports.append(
                {
                    "campaign_id": campaign["id"],
                    "report_id": report["id"],
                    "report_key": report["report_key"],
                    "title": report["title"],
                    "report_version": (report.get("summary") or {}).get("report_version"),
                    "recommendations": len(report.get("recommendations") or []),
                    "uses_inconclusive_language": "Inconclusive" in str(report.get("markdown_report") or ""),
                }
            )
        except Exception as error:  # noqa: BLE001 - evidence export should preserve failures
            errors.append({"campaign_id": campaign["id"], "error": str(error)})
    conn.commit()
    return {
        "phase": 6,
        "calculation_version": PHASE_6_REPORT_VERSION,
        "completed_campaigns": campaigns,
        "reports": reports,
        "backfill": {"campaigns": len(campaigns), "reports": len(reports), "errors": errors},
        "phase7_started": False,
        "simulation_only": True,
    }


def write_reports(payload: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "evidence.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    (REPORT_DIR / "README.md").write_text(readme(payload), encoding="utf-8")
    (REPORT_DIR / "backfill_report.md").write_text(backfill_report(payload), encoding="utf-8")
    (REPORT_DIR / "report_contract.md").write_text(report_contract(), encoding="utf-8")
    (REPORT_DIR / "architecture_assessment.md").write_text(architecture_assessment(payload), encoding="utf-8")


def readme(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Phase 6 Automated Scientific Reporting",
            "",
            f"Completed campaigns processed: `{payload['backfill']['campaigns']}`.",
            f"Scientific reports available: `{payload['backfill']['reports']}`.",
            f"Backfill errors: `{len(payload['backfill']['errors'])}`.",
            "",
            "Reports are simulation-only and preserve post-hoc/unconfirmed classifications.",
            "",
            f"Phase 7 started: `{payload['phase7_started']}`.",
        ]
    )


def backfill_report(payload: dict[str, Any]) -> str:
    lines = ["# Phase 6 Backfill Report", ""]
    lines.append("| Campaign | Report ID | Version | Recommendations | Inconclusive language |")
    lines.append("| ---: | ---: | --- | ---: | --- |")
    for row in payload["reports"]:
        lines.append(
            f"| `{row['campaign_id']}` | `{row['report_id']}` | `{row['report_version']}` | `{row['recommendations']}` | `{row['uses_inconclusive_language']}` |"
        )
    if payload["backfill"]["errors"]:
        lines.extend(["", "## Errors", "", json.dumps(payload["backfill"]["errors"], indent=2)])
    return "\n".join(lines)


def report_contract() -> str:
    return "\n".join(
        [
            "# Report Contract",
            "",
            "- Every completed campaign finalization calls the Phase 6 scientific report generator.",
            "- Manual report regeneration uses the same deterministic report path.",
            "- Reports are stored in the existing `research_campaign_reports` table.",
            "- Reports use explicit evidence references and say `Inconclusive — insufficient evidence.` when support is missing.",
            "- Post-hoc hypotheses are not reported as confirmed without independent future frozen validation.",
            "- Validation policy remains unchanged.",
        ]
    )


def architecture_assessment(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Phase 1-6 Architecture Assessment",
            "",
            "KefTrade now has a complete simulation-only research loop:",
            "",
            "```text",
            "Observe -> Hypothesize -> Generate -> Validate -> Learn -> Evolve -> Report",
            "```",
            "",
            "The implemented architecture preserves frozen datasets, generated hypotheses, strategy-family evidence, candidate-stage outcomes, failed candidates, lineage, contradictory evidence, evolution history, and automated campaign reports.",
            "",
            "The system is now ready for an empirical evaluation period rather than another feature phase.",
            "",
            f"Evidence: `{payload['backfill']['reports']}` reports generated over `{payload['backfill']['campaigns']}` completed campaigns with `{len(payload['backfill']['errors'])}` errors.",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Write Phase 6 evidence reports.")
    parser.add_argument("--write-reports", action="store_true")
    args = parser.parse_args()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        payload = collect_evidence(conn)
    if args.write_reports:
        write_reports(payload)
    print(json.dumps({"reports": len(payload["reports"]), "errors": len(payload["backfill"]["errors"]), "phase7_started": payload["phase7_started"]}, indent=2))


if __name__ == "__main__":
    main()
