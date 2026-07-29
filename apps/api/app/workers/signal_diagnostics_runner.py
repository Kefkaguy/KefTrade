"""Background worker for signal diagnostics -- same shape as campaign_runner.py.

The measurement is expensive (reads bars for every family/variant/symbol,
runs decide() over each) and used to run inline inside the API request,
holding that request's DB transaction open for 100+ seconds and starving the
single API worker's event loop of GIL time long enough for nginx to 502 on
unrelated endpoints. This process claims jobs from
research_signal_diagnostics_jobs on its own short-lived connections instead,
so the API only ever has to do one fast INSERT to enqueue.
"""

from __future__ import annotations

import argparse
import time

from app.db import connect
from app.services.signal_diagnostics import run_one_signal_diagnostics_job


def run_cycle() -> dict | None:
    with connect() as conn:
        return run_one_signal_diagnostics_job(conn)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the KefTrade signal-diagnostics worker.")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-cycles", type=int, default=None)
    args = parser.parse_args()

    cycles = 0
    while args.max_cycles is None or cycles < args.max_cycles:
        job = run_cycle()
        cycles += 1
        if job is None:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
