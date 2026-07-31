"""Static guard against undefined names in application code.

A CLI path that only runs against a real database is not covered by the unit
suite, so a stale variable left behind by a refactor reaches production and
fails at the point of use -- after the expensive work has already been done.
Ruff's F821 finds those without needing to execute anything.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def test_application_code_has_no_undefined_names() -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(APP), "--select", "F821"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:  # pragma: no cover - ruff absent in a bare env
        pytest.skip("ruff is not installed")
    if "No module named ruff" in (result.stderr or ""):
        pytest.skip("ruff is not installed")

    assert result.returncode == 0, (
        "Undefined names found in application code:\n"
        f"{result.stdout}\n{result.stderr}"
    )
