"""Publish a generated digest to Supabase (the `digests` table Lovable reads).

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment. Upserts
on the `date` column so re-runs for the same `data_as_of` overwrite cleanly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

TABLE = "digests"


def publish_digest(digest: dict) -> None:
    """Upsert one digest dict into Supabase. Raises on HTTP failure."""
    load_dotenv()
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    row = {
        "date": digest["data_as_of"],
        "generated_at": digest["generated_at"],
        "digest": digest["digest"],
        "meta": digest["meta"],
    }

    r = httpx.post(
        f"{url}/rest/v1/{TABLE}",
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
