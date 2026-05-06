"""Friday close-of-play weekly wrap orchestrator.

Pipes `digest_backend.py` (with DIGEST_CONTEXT_WINDOW_HOURS=120 so the news
context spans the full trading week, not the 24h default) into
`weekly_writer.py`, persists the JSON payload, and upserts the wrap into the
Supabase `weekly_wraps` table.

Modes:
  --now        Run unconditionally (bypasses Friday + holiday checks).
  --scheduled  One-shot for external cron (e.g. GitHub Actions). Skips if
               today (Europe/London) is not Friday; lets US market holiday
               Fridays through (a 4-day week still warrants a wrap).

The cron schedule is in .github/workflows/weekly-wrap.yml.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from publish import publish_weekly_wrap

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"
LOGS_DIR = SCRIPT_DIR / "logs"
LONDON = ZoneInfo("Europe/London")

# 5 trading days × 24h. The backend reads this from env at module load.
WEEKLY_CONTEXT_WINDOW_HOURS = "120"

log = logging.getLogger("run_weekly")


def setup_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    log.handlers.clear()

    fh = logging.FileHandler(LOGS_DIR / "run_weekly.log")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def run_pipeline() -> bool:
    """Run backend (with weekly lookback) | writer, persist + publish. True on success."""
    today = datetime.now(LONDON).date()
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"weekly_wrap_{today.isoformat()}.json"
    latest_path = OUTPUT_DIR / "weekly_wrap_latest.json"
    backend_debug_path = OUTPUT_DIR / f"weekly_backend_{today.isoformat()}.json"

    backend_env = os.environ.copy()
    backend_env["DIGEST_CONTEXT_WINDOW_HOURS"] = WEEKLY_CONTEXT_WINDOW_HOURS

    log.info("Running backend pipeline (lookback=%sh)", WEEKLY_CONTEXT_WINDOW_HOURS)
    backend = subprocess.run(
        [sys.executable, "digest_backend.py"],
        capture_output=True,
        text=True,
        cwd=SCRIPT_DIR,
        env=backend_env,
    )
    if backend.returncode != 0:
        log.error("Backend failed (rc=%s): %s", backend.returncode, backend.stderr.strip())
        return False
    if backend.stderr.strip():
        log.info("Backend stderr: %s", backend.stderr.strip())

    log.info("Running weekly writer")
    writer = subprocess.run(
        [sys.executable, "weekly_writer.py"],
        input=backend.stdout,
        capture_output=True,
        text=True,
        cwd=SCRIPT_DIR,
    )
    if writer.returncode != 0:
        backend_debug_path.write_text(backend.stdout)
        log.error(
            "Writer failed (rc=%s). Backend output saved to %s. stderr: %s",
            writer.returncode, backend_debug_path, writer.stderr.strip(),
        )
        return False
    if writer.stderr.strip():
        log.info("Writer stderr: %s", writer.stderr.strip())

    wrap = json.loads(writer.stdout)

    out_path.write_text(writer.stdout)
    shutil.copyfile(out_path, latest_path)
    log.info("Wrap saved to %s (%d bytes)", out_path, len(writer.stdout))

    try:
        publish_weekly_wrap(wrap)
        log.info("Published to Supabase (week_ending=%s)", wrap.get("week_ending"))
    except Exception as exc:
        log.exception("Supabase publish failed (local file still saved): %s", exc)

    return True


def scheduled_job() -> None:
    today = datetime.now(LONDON).date()
    if today.weekday() != 4:  # Mon=0..Fri=4..Sun=6
        log.info("Skipping %s: not Friday (weekday=%d)", today.isoformat(), today.weekday())
        return
    log.info("Starting weekly run for %s", today.isoformat())
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


def run_scheduled_once() -> int:
    log.info("External cron --scheduled run triggered")
    try:
        scheduled_job()
        return 0
    except Exception as exc:
        log.exception("Scheduled run raised: %s", exc)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Friday weekly wrap scheduler")
    parser.add_argument("--now", action="store_true",
        help="Run unconditionally, bypassing the Friday check, then exit.")
    parser.add_argument("--scheduled", action="store_true",
        help="One-shot run for external cron. Skips if today is not Friday.")
    args = parser.parse_args()

    setup_logging()

    if args.now:
        return run_now()
    if args.scheduled:
        return run_scheduled_once()
    parser.error("Specify --now or --scheduled")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
