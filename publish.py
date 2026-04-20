"""Publish generated content to Supabase (the tables Lovable reads from).

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment.

- `publish_digest`: upserts the daily digest into `digests` keyed on `date`.
- `publish_guidance`: upserts a company guidance row into `company_guidance`
  keyed on (ticker, fiscal_period). Used so the next quarter's earnings
  pipeline can reference the current-quarter guidance as `prior_guidance`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

DIGESTS_TABLE = "digests"
GUIDANCE_TABLE = "company_guidance"
EARNINGS_CARDS_TABLE = "earnings_cards"
COMPANY_PROFILES_TABLE = "company_profiles"


def _supabase_env() -> tuple[str, str]:
    load_dotenv(override=True)
    return os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_ROLE_KEY"]


def publish_digest(digest: dict) -> None:
    """Upsert one digest dict into Supabase. Raises on HTTP failure."""
    url, key = _supabase_env()
    row = {
        "date": digest["data_as_of"],
        "generated_at": digest["generated_at"],
        "digest": digest["digest"],
        "meta": digest["meta"],
    }
    r = httpx.post(
        f"{url}/rest/v1/{DIGESTS_TABLE}",
        params={"on_conflict": "date"},
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=row,
        timeout=30,
    )
    r.raise_for_status()


def publish_earnings_card(card: dict) -> None:
    """Upsert an earnings card into the dedicated table. Raises on HTTP failure.

    The card's `_source.filed_at` is lifted to a top-level column so Lovable can
    filter by filing date without parsing jsonb. `(ticker, fiscal_period)` is
    the conflict key, so re-runs overwrite cleanly.
    """
    url, key = _supabase_env()
    source = card.get("_source") or {}
    filed_at = source.get("filed_at")
    if not filed_at:
        raise ValueError(f"earnings card missing _source.filed_at: {card.get('ticker')}")
    row = {
        "ticker": card["ticker"],
        "fiscal_period": card["fiscal_period"],
        "filed_at": filed_at,
        "card": card,
    }
    r = httpx.post(
        f"{url}/rest/v1/{EARNINGS_CARDS_TABLE}",
        params={"on_conflict": "ticker,fiscal_period"},
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=row,
        timeout=30,
    )
    r.raise_for_status()


def publish_company_profile(profile: dict) -> None:
    """Upsert a company profile. Raises on HTTP failure.

    Expects the dict shape produced by company_profile_builder.build_profile().
    Conflict key is `ticker` — re-runs overwrite prior profiles cleanly.
    """
    url, key = _supabase_env()
    r = httpx.post(
        f"{url}/rest/v1/{COMPANY_PROFILES_TABLE}",
        params={"on_conflict": "ticker"},
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=profile,
        timeout=30,
    )
    r.raise_for_status()


def publish_guidance(ticker: str, fiscal_period: str, guidance: dict, filed_at: str) -> None:
    """Upsert a company_guidance row (ticker, fiscal_period). Raises on HTTP failure."""
    url, key = _supabase_env()
    row = {
        "ticker": ticker,
        "fiscal_period": fiscal_period,
        "guidance": guidance,
        "filed_at": filed_at,
    }
    r = httpx.post(
        f"{url}/rest/v1/{GUIDANCE_TABLE}",
        params={"on_conflict": "ticker,fiscal_period"},
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=row,
        timeout=30,
    )
    r.raise_for_status()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Publish a digest JSON file to Supabase")
    parser.add_argument("path", type=Path, help="Path to digest JSON (writer output shape)")
    args = parser.parse_args()

    digest = json.loads(args.path.read_text())
    publish_digest(digest)
    print(f"Published {args.path} (date={digest['data_as_of']}) to Supabase")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
