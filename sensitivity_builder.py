"""Compute rolling macro sensitivities for each watchlist ticker: how
strongly does the stock's daily returns correlate with changes in key
macro variables (yields, FX, commodities, credit spreads, VIX)?

Pure Python/pandas. No Claude calls, no LLM cost.

Pipeline per ticker:
    1. Fetch ~3 years of daily close prices from yfinance.
    2. For each FRED series, fetch daily values; compute daily change
       (first-difference for yields/spreads/VIX; pct return for price
       series like oil and gold).
    3. Align dates, drop missing, compute correlation + regression beta
       + R^2. Derive direction ("positive" / "negative" / "mixed") and
       magnitude ("strong" / "moderate" / "weak") for display.
    4. Upsert all pairs to Supabase.

Run quarterly — sensitivities shift slowly. Rerunning more often is
harmless (idempotent upsert) but not informative.

Usage:
    python sensitivity_builder.py                   # all watchlist tickers
    python sensitivity_builder.py --ticker MSFT     # single ticker
    python sensitivity_builder.py --no-publish      # dry run, print rows
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import yfinance as yf
from dotenv import load_dotenv

from company_profile_builder import flat_watchlist

# Macro series curated for what matters to a buy-side fund manager.
# Each entry: (series_id, human_name, kind, source). Source is "fred" or
# "yf" — FRED was the first choice but its LBMA gold series was
# deprecated post-2022 licensing changes, so we fetch gold via yfinance
# futures instead. kind determines how we transform the series:
#   "yield" -> first-difference (daily change in bps-equivalent)
#   "price" -> daily pct return (log-return equivalent)
#   "level" -> first-difference (for indices like VIX, DXY where the raw
#              level is more meaningful than a return)
SENSITIVITY_SERIES: list[tuple[str, str, str, str]] = [
    ("DGS10",                 "US 10Y Treasury yield",      "yield", "fred"),
    ("DGS2",                  "US 2Y Treasury yield",       "yield", "fred"),
    ("DTWEXBGS",              "Trade-weighted USD",         "level", "fred"),
    ("DCOILBRENTEU",          "Brent crude oil",            "price", "fred"),
    ("GC=F",                  "Gold futures",               "price", "yf"),
    ("BAMLH0A0HYM2",          "US HY credit spread (OAS)",  "yield", "fred"),
    ("VIXCLS",                "VIX (S&P 500 implied vol)",  "level", "fred"),
]

LOOKBACK_DAYS = 3 * 365  # ~3 years of daily observations
MIN_OBSERVATIONS = 250   # require at least 1 year of overlap


def _classify(correlation: float) -> tuple[str, str]:
    """Return (direction, magnitude) for display — the numeric fields are
    still authoritative; these are shorthand for Lovable labels."""
    abs_c = abs(correlation)
    if abs_c < 0.15:
        return "neutral", "weak"
    if abs_c < 0.35:
        direction = "positive" if correlation > 0 else "negative"
        return direction, "moderate"
    direction = "positive" if correlation > 0 else "negative"
    return direction, "strong"


def _stock_returns(ticker: str) -> pd.Series | None:
    """Return a daily pct-return Series indexed by date (naive), or None."""
    end = datetime.now(timezone.utc).date()
    start = end - pd.Timedelta(days=LOOKBACK_DAYS + 30)  # a bit of slack
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
    except Exception as exc:
        print(f"[sens] {ticker}: yfinance history failed: {exc}", file=sys.stderr)
        return None
    if hist is None or hist.empty:
        return None
    closes = hist["Close"].copy()
    closes.index = pd.DatetimeIndex(closes.index).tz_localize(None).normalize()
    returns = closes.pct_change().dropna()
    returns.name = ticker
    return returns


def _transform(raw: pd.Series, kind: str) -> pd.Series:
    raw = raw.astype(float).dropna()
    raw.index = pd.DatetimeIndex(raw.index).normalize()
    if kind == "price":
        return raw.pct_change().dropna()
    return raw.diff().dropna()


def _fred_series_transformed(fred, series_id: str, kind: str) -> pd.Series | None:
    try:
        raw = fred.get_series(series_id).dropna()
    except Exception as exc:
        print(f"[sens] FRED {series_id}: fetch failed: {exc}", file=sys.stderr)
        return None
    out = _transform(raw, kind)
    out.name = series_id
    return out


def _yf_series_transformed(yf_symbol: str, kind: str) -> pd.Series | None:
    """Fallback for macro series FRED no longer carries (e.g., gold)."""
    end = datetime.now(timezone.utc).date()
    start = end - pd.Timedelta(days=LOOKBACK_DAYS + 30)
    try:
        hist = yf.Ticker(yf_symbol).history(start=start, end=end, auto_adjust=True)
    except Exception as exc:
        print(f"[sens] yfinance {yf_symbol}: fetch failed: {exc}", file=sys.stderr)
        return None
    if hist is None or hist.empty:
        return None
    closes = hist["Close"].copy()
    closes.index = pd.DatetimeIndex(closes.index).tz_localize(None).normalize()
    out = _transform(closes, kind)
    out.name = yf_symbol
    return out


def compute_sensitivity(
    ticker: str,
    returns: pd.Series,
    macro: pd.Series,
    series_id: str,
    series_name: str,
    window_days: int,
) -> dict | None:
    """Align dates, compute correlation + OLS beta + R^2."""
    joined = pd.concat([returns, macro], axis=1, join="inner").dropna()
    if len(joined) < MIN_OBSERVATIONS:
        return None
    # Trim to the last `window_days` calendar days so we compute over a
    # consistent rolling window rather than the full intersection.
    cutoff = joined.index.max() - pd.Timedelta(days=window_days)
    joined = joined[joined.index >= cutoff]
    if len(joined) < MIN_OBSERVATIONS:
        return None

    x = joined.iloc[:, 1].to_numpy()
    y = joined.iloc[:, 0].to_numpy()
    if np.std(x) == 0 or np.std(y) == 0:
        return None
    corr = float(np.corrcoef(x, y)[0, 1])
    # Simple OLS: beta = cov(x,y)/var(x), r2 = corr^2
    beta = float(np.cov(x, y, ddof=0)[0, 1] / np.var(x))
    r_sq = corr ** 2
    direction, magnitude = _classify(corr)
    return {
        "ticker":         ticker,
        "series_id":      series_id,
        "series_name":    series_name,
        "correlation":    round(corr, 4),
        "beta":           round(beta, 6),
        "r_squared":      round(r_sq, 4),
        "n_observations": int(len(joined)),
        "window_days":    window_days,
        "direction":      direction,
        "magnitude":      magnitude,
        "computed_at":    datetime.now(timezone.utc).isoformat(),
    }


def build_ticker_sensitivities(
    ticker: str,
    fred,
    series_list: list[tuple[str, str, str, str]],
    window_days: int,
    macro_cache: dict[tuple[str, str], pd.Series],
) -> list[dict]:
    returns = _stock_returns(ticker)
    if returns is None or returns.empty:
        print(f"[sens] {ticker}: no return series available", file=sys.stderr)
        return []
    rows = []
    for series_id, name, kind, source in series_list:
        cache_key = (series_id, source)
        if cache_key in macro_cache:
            macro = macro_cache[cache_key]
        else:
            macro = (
                _fred_series_transformed(fred, series_id, kind) if source == "fred"
                else _yf_series_transformed(series_id, kind)
            )
            macro_cache[cache_key] = macro
        if macro is None:
            continue
        row = compute_sensitivity(ticker, returns, macro, series_id, name, window_days)
        if row:
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute macro sensitivities for each watchlist ticker")
    parser.add_argument("--ticker", help="Process only this ticker")
    parser.add_argument("--no-publish", action="store_true", help="Skip Supabase upsert; print rows")
    parser.add_argument("--window-days", type=int, default=LOOKBACK_DAYS)
    args = parser.parse_args()

    load_dotenv(override=True)
    fred_key = os.environ.get("FRED_API_KEY")
    if not fred_key:
        print("sensitivity_builder: FRED_API_KEY not set", file=sys.stderr)
        return 1
    try:
        from fredapi import Fred
    except ImportError:
        print("sensitivity_builder: fredapi not installed", file=sys.stderr)
        return 1
    fred = Fred(api_key=fred_key)

    targets = flat_watchlist()
    if args.ticker:
        targets = [t for t in targets if t[1].upper() == args.ticker.upper()]
        if not targets:
            print(f"ticker {args.ticker} not in watchlist", file=sys.stderr)
            return 1

    from publish import publish_macro_sensitivities

    all_rows: list[dict] = []
    macro_cache: dict[tuple[str, str], pd.Series] = {}  # reuse macro series across tickers
    for _, ticker, _ in targets:
        rows = build_ticker_sensitivities(ticker, fred, SENSITIVITY_SERIES, args.window_days, macro_cache)
        if rows:
            print(f"[sens] {ticker}: {len(rows)} series computed", file=sys.stderr)
        all_rows.extend(rows)

    if args.no_publish:
        print(json.dumps(all_rows, indent=2))
    else:
        try:
            publish_macro_sensitivities(all_rows)
        except Exception as exc:
            print(f"[sens] publish failed: {exc}", file=sys.stderr)
            return 1

    print(
        f"[sens] done: {len(all_rows)} row(s) across {len(targets)} ticker(s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
