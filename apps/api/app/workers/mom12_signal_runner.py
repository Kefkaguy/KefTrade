"""Run the frozen MOM_12_1 forward generator on a quiet, durable schedule."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

from app.settings import settings

logger = logging.getLogger("keftrade.mom12_signal_worker")


def run_once() -> int:
    script = Path(settings.mom_12_1_generator_script)
    if not script.is_file():
        raise FileNotFoundError(f"frozen MOM_12_1 generator is missing: {script}")
    completed = subprocess.run(
        [sys.executable, str(script)],
        check=False,
    )
    if completed.returncode:
        logger.error("MOM_12_1 generator failed returncode=%s", completed.returncode)
    else:
        logger.info("MOM_12_1 generator cycle complete")
    return completed.returncode


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("MOM_12_1 generator cycle failed")
        time.sleep(max(300, settings.mom_12_1_generator_poll_seconds))


if __name__ == "__main__":
    main()
