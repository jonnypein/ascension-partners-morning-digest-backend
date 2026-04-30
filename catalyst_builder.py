"""Pull upcoming (and very recent) earnings dates for every watchlist ticker
from yfinance and upsert to Supabase `catalysts` table.

No Claude calls, no SEC fetches — this is a pure data refresh, fast and
free. Non-earnings catalysts (analyst days, product launches, regulatory
decisions) can be inserted manually into the table later; this builder
only writes rows with source='yfinance' so it won't clobber them.

Usage:
    python catalyst_builder.py                   # all watchlist tickers
    python catalyst_builder.py --ticker MSFT     # single ticker
    python catalyst_builder.py --no-publish      # dry run, print rows

Window: by default we include earnings dates from 30 days ago onwards,
so the Lovable UI can show the most recent print as "last reported" and
any future scheduled prints for upcoming catalysts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from company_profile_builder import flat_watchlist

DEFAULT_BACKWARD_DAYS = 30
DEFAULT_FORWARD_DAYS = 180


def fetch_earnings_catalysts(
    ticker: str,
    backward_days: int = DEFAULT_BACKWARD_DAYS,
    forward_days: int = DEFAULT_FORWARD_DAYS,
) -> list[dict]:
    """Return upcoming + recent earnings dates for `ticker` as catalyst rows."""
    try:
        ed = yf.Ticker(ticker).earnings_dates
    except Exception as exc:
        print(f"[catalyst] {ticker}: earnings_dates fetch failed: {exc}", file=sys.stderr)
        return []
    if ed is None or ed.empty:
        return []

    now = datetime.now(timezone.utc)
    cutoff_back = now - timedelta(days=backward_days)
    cutoff_forward = now + timedelta(days=forward_days)

    rows = []
    for raw_ts, row in ed.iterrows():
        # yfinance gives a tz-aware pandas Timestamp (usually America/New_York).
        try:
            ts = pd.Timestamp(raw_ts).tz_convert("UTC")
        except Exception:
            try:
                ts = pd.Timestamp(raw_ts).tz_localize("UTC")
            except Exception:
                continue
        if ts < cutoff_back or ts > cutoff_forward:
            continue

        # EPS estimate is often present; actual EPS populated after a print.
        parts = []
        eps_est = row.get("EPS Estimate")
        eps_actual = row.get("Reported EPS")
        if pd.notna(eps_est):
            parts.append(f"EPS est {eps_est:.2f}")
        if pd.notna(eps_actual):
            parts.append(f"reported {eps_actual:.2f}")
        description = "; ".join(parts) if parts else "Scheduled earnings release"

        rows.append({
            "ticker":      ticker,
            "event_date":  ts.date().isoformat(),
            "event_type":  "earnings",
            "description": description,
            "source":      "yfinance",
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh earnings-date catalysts from yfinance")
    parser.add_argument("--ticker", help="Process only this ticker")
    parser.add_argument("--no-publish", action="store_true", help="Skip Supabase upsert; print rows")
    parser.add_argument("--backward-days", type=int, default=DEFAULT_BACKWARD_DAYS)
    parser.add_argument("--forward-days", type=int, default=DEFAULT_FORWARD_DAYS)
    args = parser.parse_args()

    load_dotenv(override=True)

    targets = flat_watchlist()
    if args.ticker:
        targets = [t for t in targets if t[1].upper() == args.ticker.upper()]
        if not targets:
            print(f"ticker {args.ticker} not in watchlist", file=sys.stderr)
            return 1

    from publish import publish_catalysts

    all_rows: list[dict] = []
    per_ticker_counts: dict[str, int] = {}
    for _, ticker, _ in targets:
        rows = fetch_earnings_catalysts(
            ticker,
            backward_days=args.backward_days,
            forward_days=args.forward_days,
        )
        per_ticker_counts[ticker] = len(rows)
        all_rows.extend(rows)

    if args.no_publish:
        print(json.dumps(all_rows, indent=2))
    else:
        try:
            publish_catalysts(all_rows)
        except Exception as exc:
            print(f"[catalyst] publish failed: {exc}", file=sys.stderr)
            return 1

    total = len(all_rows)
    missed = sum(1 for n in per_ticker_counts.values() if n == 0)
    print(
        f"[catalyst] done: {total} row(s) upserted across {len(per_ticker_counts)} ticker(s); "
        f"{missed} ticker(s) returned no data",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
