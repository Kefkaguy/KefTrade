"""Import licensed/backend research evidence on the VPS.

The command accepts either a JSON array or JSON Lines. It intentionally does
not scrape or infer auction, membership, or corporate-action evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.db import connect
from app.services.intraday_research_data import (
    persist_auction_imbalances,
    persist_corporate_actions,
    persist_point_in_time_membership,
)


def _load_rows(path: Path) -> list[dict[str, Any]]:
    content = path.read_text(encoding="utf-8")
    stripped = content.lstrip()
    if not stripped:
        return []
    parsed = json.loads(content) if stripped.startswith("[") else [
        json.loads(line)
        for line in content.splitlines()
        if line.strip()
    ]
    if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
        raise ValueError("research evidence must be a JSON array or JSON Lines of objects")
    return parsed


def execute(args: argparse.Namespace) -> dict[str, Any]:
    rows = _load_rows(args.file)
    writers = {
        "auction": persist_auction_imbalances,
        "universe": persist_point_in_time_membership,
        "corporate-actions": persist_corporate_actions,
    }
    with connect() as conn:
        inserted = writers[args.kind](conn, rows)
        conn.commit()
    return {
        "kind": args.kind,
        "source_file": str(args.file),
        "received": len(rows),
        "inserted": inserted,
        "duplicates_ignored": len(rows) - inserted,
        "backend_only": True,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Import institutional intraday research evidence from JSON/JSONL."
    )
    root.add_argument(
        "--kind",
        choices=("auction", "universe", "corporate-actions"),
        required=True,
    )
    root.add_argument("--file", type=Path, required=True)
    return root


def main() -> None:
    print("Intraday research evidence import | backend only", flush=True)
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()
