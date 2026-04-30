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

from publish import publish_digest, publish_earnings_card, publish_guidance

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

    digest = json.loads(writer.stdout)

    out_path.write_text(writer.stdout)
    shutil.copyfile(out_path, latest_path)
    log.info("Digest saved to %s (%d bytes)", out_path, len(writer.stdout))

    # Earnings pipeline runs independently of the daily digest row. Each card
    # is upserted into its own `earnings_cards` table keyed by (ticker,
    # fiscal_period), so cards surface on their actual filing date in Lovable
    # rather than being pinned to whichever daily run generated them.
    try:
        earnings_payload = run_earnings_pipeline()
        earnings_cards = earnings_payload.get("earnings_cards", [])
        meta = earnings_payload.get("meta", {})
        if earnings_payload:
            # Persist the writer's full payload (cards + meta) so the artifact
            # exposes bundles_in / cards_out / warnings — visible without
            # grepping workflow logs. Added after the Apr 24 silent-drop
            # incident.
            earnings_path = OUTPUT_DIR / f"earnings_cards_{today.isoformat()}.json"
            earnings_path.write_text(json.dumps(earnings_payload))
            bundles_in = meta.get("bundles_in")
            cards_out = meta.get("cards_out", len(earnings_cards))
            if bundles_in and cards_out != bundles_in:
                log.warning(
                    "Earnings writer dropped %d/%d bundle(s); see meta.warnings: %s",
                    bundles_in - cards_out, bundles_in, meta.get("warnings"),
                )
            log.info("Earnings pipeline produced %d card(s)", len(earnings_cards))
            for card in earnings_cards:
                _publish_card(card)
                _persist_card_guidance(card)
    except Exception as exc:
        log.exception("Earnings pipeline failed (digest still saved): %s", exc)

    try:
        publish_digest(digest)
        log.info("Published to Supabase (date=%s)", digest.get("data_as_of"))
    except Exception as exc:
        log.exception("Supabase publish failed (local file still saved): %s", exc)

    return True


PERSISTABLE_GUIDANCE = {"raised", "lowered", "maintained", "initiated"}


def run_earnings_pipeline() -> dict:
    """Pipe earnings_backend.py into earnings_writer.py and return the writer's full payload.

    Returns the writer's JSON payload (`{generated_at, earnings_cards, meta}`)
    so callers can inspect `meta.bundles_in / cards_out / warnings`.
    Returns `{}` on any failure so the caller can short-circuit.
    """
    log.info("Running earnings backend")
    backend = subprocess.run(
        [sys.executable, "earnings_backend.py"],
        capture_output=True,
        text=True,
        cwd=SCRIPT_DIR,
    )
    if backend.returncode != 0:
        log.error("Earnings backend failed (rc=%s): %s", backend.returncode, backend.stderr.strip())
        return {}
    if backend.stderr.strip():
        log.info("Earnings backend stderr: %s", backend.stderr.strip())

    # Short-circuit if there are no bundles — skip the Claude call entirely.
    bundles = json.loads(backend.stdout).get("bundles", [])
    if not bundles:
        log.info("Earnings backend: no recent 8-Ks in watchlist")
        return {}

    log.info("Running earnings writer (%d bundle(s))", len(bundles))
    writer = subprocess.run(
        [sys.executable, "earnings_writer.py"],
        input=backend.stdout,
        capture_output=True,
        text=True,
        cwd=SCRIPT_DIR,
    )
    if writer.returncode != 0:
        log.error("Earnings writer failed (rc=%s): %s", writer.returncode, writer.stderr.strip())
        return {}
    if writer.stderr.strip():
        log.info("Earnings writer stderr: %s", writer.stderr.strip())

    return json.loads(writer.stdout)


def _publish_card(card: dict) -> None:
    try:
        publish_earnings_card(card)
        log.info("Published earnings card: %s %s", card.get("ticker"), card.get("fiscal_period"))
    except Exception as exc:
        log.exception("publish_earnings_card failed for %s: %s", card.get("ticker"), exc)


def _persist_card_guidance(card: dict) -> None:
    guidance = card.get("guidance") or {}
    direction = guidance.get("direction")
    if direction not in PERSISTABLE_GUIDANCE:
        return
    source = card.get("_source") or {}
    filed_at = source.get("filed_at")
    fiscal_period = card.get("fiscal_period")
    ticker = card.get("ticker")
    if not (ticker and fiscal_period and filed_at):
        log.warning("Skipping guidance persist for %s: incomplete card", ticker)
        return
    try:
        publish_guidance(ticker, fiscal_period, guidance, filed_at)
        log.info("Saved guidance for %s %s (direction=%s)", ticker, fiscal_period, direction)
    except Exception as exc:
        log.exception("publish_guidance failed for %s: %s", ticker, exc)


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


def run_scheduled_once() -> int:
    """One-shot scheduled run for external cron (e.g. GitHub Actions).

    Honours the NYSE holiday/weekend check inside `scheduled_job`, so the
    cron can fire Mon-Fri without us needing to hard-code a holiday list.
    """
    log.info("External cron --scheduled run triggered")
    try:
        scheduled_job()
        return 0
    except Exception as exc:
        log.exception("Scheduled run raised: %s", exc)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily Morning Digest scheduler")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run the pipeline once immediately, bypassing schedule and holiday checks, then exit.",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="One-shot run for external cron (e.g. GitHub Actions). Respects NYSE holiday/weekend skip.",
    )
    args = parser.parse_args()

    setup_logging()

    if args.now:
        return run_now()
    if args.scheduled:
        return run_scheduled_once()

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
