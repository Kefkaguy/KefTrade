"""Report a governance refusal as a result, not as a crash.

Every guard in this pipeline refuses by raising: a family built on a discovery
run, a factor run against a retired version, a snapshot with blended feeds.
Those refusals are the machinery working, and a stack trace makes them
indistinguishable from a defect -- which matters most in exactly the case
where it is least visible, a detached container writing to a log file that
someone reads hours later.

A refusal exits non-zero, so a shell loop still stops on it. It just says what
happened first.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

REFUSED_EXIT_CODE = 2


def run_command(
    command: Callable[[Any], Any], args: Any, *, banner: str
) -> None:
    """Run one CLI command, printing a refusal as structured output."""
    print(banner, flush=True)
    try:
        result = command(args)
    except ValueError as refusal:
        print(
            json.dumps(
                {
                    "refused": True,
                    "command": getattr(args, "command", None),
                    "reason": str(refusal),
                },
                indent=2,
            )
        )
        sys.exit(REFUSED_EXIT_CODE)
    print(json.dumps(result, default=str, indent=2))
