"""Capture a point-in-time snapshot of sell-side consensus for each
watchlist ticker: forward estimates (revenue, EPS), price target
distribution, and recommendation split. Feeds the Consensus section
of the `/companies/:ticker` Lovable page.

Pure yfinance pulls — no Claude, no SEC, free.

Run weekly. Each run upserts one row per ticker keyed on
`(ticker, asof_date)`, so re-running on the same day is idempotent
but distinct days accumulate as historical revisions data. Once a
few months' worth of snapshots exist, Lovable can render revision
trends ("estimates up 3% over 90 days").

Usage:
    python consensus_builder.py                    # all watchlist tickers
    python consensus_builder.py --ticker MSFT      # single ticker
    python consensus_builder.py --no-publish       # dry run, print rows
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from company_profile_builder import flat_watchlist


def _estimates_to_dict(df) -> dict:
    """Convert yfinance's revenue/earnings_estimate DataFrame to
    {period -> {avg, low, high, n, growth}}. Missing fields become null."""
    if df is None or getattr(df, "empty", True):
        return {}
    out: dict[str, dict] = {}
    for period, row in df.iterrows():
        avg = row.get("avg")
        low = row.get("low")
        high = row.get("high")
        n = row.get("numberOfAnalysts")
        growth = row.get("growth")
        out[str(period)] = {
            "avg":    None if pd.isna(avg) else float(avg),
            "low":    None if pd.isna(low) else float(low),
            "high":   None if pd.isna(high) else float(high),
            "n":      None if pd.isna(n) else int(n),
            "growth": None if pd.isna(growth) else float(growth),
        }
    return out


def _price_targets(ticker: yf.Ticker) -> dict:
    try:
        pt = ticker.analyst_price_targets
    except Exception as exc:
        print(f"[consensus] {ticker.ticker}: price targets fetch failed: {exc}", file=sys.stderr)
        return {}
    if not isinstance(pt, dict):
        return {}
    return {
        "current": pt.get("current"),
        "low":     pt.get("low"),
        "high":    pt.get("high"),
        "mean":    pt.get("mean"),
        "median":  pt.get("median"),
    }


def _recommendations(ticker: yf.Ticker) -> dict:
    try:
        rs = ticker.recommendations_summary
    except Exception as exc:
        print(f"[consensus] {ticker.ticker}: recommendations fetch failed: {exc}", file=sys.stderr)
        return {}
    if rs is None or rs.empty:
        return {}
    # 0m row is the most-recent month's distribution; prior rows enable
    # revision trend visualisation later but we store only 0m for now.
    try:
        current = rs[rs["period"] == "0m"].iloc[0]
    except (IndexError, KeyError):
        current = rs.iloc[0]
    total = int(current.get("strongBuy", 0) + current.get("buy", 0)
                + current.get("hold", 0) + current.get("sell", 0)
                + current.get("strongSell", 0))
    return {
        "strong_buy":  int(current.get("strongBuy", 0)),
        "buy":         int(current.get("buy", 0)),
        "hold":        int(current.get("hold", 0)),
        "sell":        int(current.get("sell", 0)),
        "strong_sell": int(current.get("strongSell", 0)),
        "total":       total,
    }


def build_snapshot(ticker_sym: str, asof_date: str) -> dict | None:
    t = yf.Ticker(ticker_sym)
    try:
        revenue = _estimates_to_dict(t.revenue_estimate)
        earnings = _estimates_to_dict(t.earnings_estimate)
    except Exception as exc:
        print(f"[consensus] {ticker_sym}: estimate fetch failed: {exc}", file=sys.stderr)
        revenue, earnings = {}, {}
    price_targets = _price_targets(t)
    recommendations = _recommendations(t)

    if not (revenue or earnings or price_targets or recommendations):
        return None

    return {
        "ticker":            ticker_sym,
        "asof_date":         asof_date,
        "revenue_estimates": revenue or None,
        "eps_estimates":     earnings or None,
        "price_targets":     price_targets or None,
        "recommendations":   recommendations or None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot sell-side consensus for each watchlist ticker")
    parser.add_argument("--ticker", help="Process only this ticker")
    parser.add_argument("--no-publish", action="store_true", help="Skip Supabase upsert; print rows")
    parser.add_argument("--asof", help="Override asof_date (YYYY-MM-DD). Defaults to today UTC.")
    args = parser.parse_args()

    load_dotenv(override=True)

    asof = args.asof or datetime.now(timezone.utc).date().isoformat()

    targets = flat_watchlist()
    if args.ticker:
        targets = [t for t in targets if t[1].upper() == args.ticker.upper()]
        if not targets:
            print(f"ticker {args.ticker} not in watchlist", file=sys.stderr)
            return 1

    from publish import publish_consensus_snapshot

    ok = 0
    fail = 0
    for _, ticker, _ in targets:
        snap = build_snapshot(ticker, asof)
        if not snap:
            print(f"[consensus] {ticker}: no data available, skipping", file=sys.stderr)
            fail += 1
            continue
        if args.no_publish:
            print(json.dumps(snap, indent=2))
            ok += 1
            continue
        try:
            publish_consensus_snapshot(snap)
            print(f"[consensus] {ticker}: upserted for {asof}", file=sys.stderr)
            ok += 1
        except Exception as exc:
            print(f"[consensus] {ticker}: publish failed: {exc}", file=sys.stderr)
            fail += 1

    print(f"[consensus] done: {ok} ok / {fail} failed for asof {asof}", file=sys.stderr)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
