"""Daily scheduler for the Morning Digest pipeline.

Runs `digest_backend.py | digest_writer.py` at 05:30 Europe/London on weekdays,
skipping NYSE holidays. Persists output to `output/digest_YYYY-MM-DD.json` and
`output/digest_latest.json`. Logs to `logs/run_daily.log` and stdout.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import schedule

from publish import publish_digest

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"
LOGS_DIR = SCRIPT_DIR / "logs"
LONDON = ZoneInfo("Europe/London")
RUN_TIME = "05:30"
NYSE = mcal.get_calendar("NYSE")

log = logging.getLogger("run_daily")


def setup_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    log.handlers.clear()

    fh = logging.FileHandler(LOGS_DIR / "run_daily.log")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def market_closure_reason(day: date) -> str | None:
    """Return a reason string if the NYSE is closed on `day`, else None."""
    if day.weekday() >= 5:
        return f"weekend ({day.strftime('%A')})"
    sched = NYSE.schedule(start_date=day, end_date=day)
    if sched.empty:
        return "NYSE holiday"
    return None


def run_pipeline() -> bool:
    """Run backend | writer, persist output. Return True on success."""
    today = datetime.now(LONDON).date()
    out_path = OUTPUT_DIR / f"digest_{today.isoformat()}.json"
    latest_path = OUTPUT_DIR / "digest_latest.json"
    backend_debug_path = OUTPUT_DIR / f"backend_{today.isoformat()}.json"
    OUTPUT_DIR.mkdir(exist_ok=True)

    log.info("Running backend pipeline")
    backend = subprocess.run(
        [sys.executable, "digest_backend.py"],
        capture_output=True,
        text=True,
        cwd=SCRIPT_DIR,
    )
    if backend.returncode != 0:
        log.error("Backend failed (rc=%s): %s", backend.returncode, backend.stderr.strip())
        return False
    if backend.stderr.strip():
        log.info("Backend stderr: %s", backend.stderr.strip())

    log.info("Running writer pipeline")
    writer = subprocess.run(
        [sys.executable, "digest_writer.py"],
        input=backend.stdout,
        capture_output=True,
        text=True,
        cwd=SCRIPT_DIR,
    )
    if writer.returncode != 0:
        backend_debug_path.write_text(backend.stdout)
        log.error(
            "Writer failed (rc=%s). Backend output saved to %s. stderr: %s",
            writer.returncode,
            backend_debug_path,
            writer.stderr.strip(),
        )
        return False
    if writer.stderr.strip():
        log.info("Writer stderr: %s", writer.stderr.strip())

    out_path.write_text(writer.stdout)
    shutil.copyfile(out_path, latest_path)
    log.info("Digest saved to %s (%d bytes)", out_path, len(writer.stdout))

    try:
        digest = json.loads(writer.stdout)
        publish_digest(digest)
        log.info("Published to Supabase (date=%s)", digest.get("data_as_of"))
    except Exception as exc:
        log.exception("Supabase publish failed (local file still saved): %s", exc)

    return True


def scheduled_job() -> None:
    today = datetime.now(LONDON).date()
    reason = market_closure_reason(today)
    if reason:
        log.info("Skipping %s: %s", today.isoformat(), reason)
        return
    log.info("Starting scheduled run for %s", today.isoformat())
    try:
        ok = run_pipeline()
        log.info("Run %s", "succeeded" if ok else "failed")
    except Exception as exc:
        log.exception("Run raised: %s", exc)


def run_now() -> int:
    log.info("Manual --now run triggered")
    try:
        ok = run_pipeline()
        log.info("Manual run %s", "succeeded" if ok else "failed")
        return 0 if ok else 1
    except Exception as exc:
        log.exception("Manual run raised: %s", exc)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily Morning Digest scheduler")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run the pipeline once immediately, bypassing schedule and holiday checks, then exit.",
    )
    args = parser.parse_args()

    setup_logging()

    if args.now:
        return run_now()

    for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        getattr(schedule.every(), day).at(RUN_TIME, LONDON).do(scheduled_job)

    log.info(
        "Scheduler started: weekdays at %s Europe/London. Next run: %s",
        RUN_TIME,
        schedule.next_run(),
    )
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
