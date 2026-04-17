#!/usr/bin/env python3
"""Daily market data puller — outputs structured JSON to stdout, summary to stderr."""

import json
import os
import ssl
import sys
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

# Fix SSL certificate verification on macOS when the system bundle is missing.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

# ── Asset definitions ──────────────────────────────────────────────────────────

INDICES = [
    ("S&P 500", "^GSPC"),
    ("Nasdaq Composite", "^IXIC"),
    ("Dow Jones Industrial Average", "^DJI"),
    ("Russell 2000", "^RUT"),
    ("STOXX Europe 600", "^STOXX"),
    ("FTSE 100", "^FTSE"),
    ("DAX", "^GDAXI"),
    ("CAC 40", "^FCHI"),
    ("Nikkei 225", "^N225"),
    ("Hang Seng", "^HSI"),
    ("Shanghai Composite", "000001.SS"),
]

US_SECTORS = [
    ("Technology", "XLK"),
    ("Financials", "XLF"),
    ("Energy", "XLE"),
    ("Healthcare", "XLV"),
    ("Industrials", "XLI"),
    ("Consumer Discretionary", "XLY"),
    ("Consumer Staples", "XLP"),
    ("Utilities", "XLU"),
    ("Materials", "XLB"),
    ("Real Estate", "XLRE"),
    ("Communication Services", "XLC"),
]

WATCHLIST: dict[str, list[tuple[str, str]]] = {
    "technology_software": [
        ("Amazon", "AMZN"),
        ("Microsoft", "MSFT"),
        ("Alphabet", "GOOGL"),
        ("Meta", "META"),
        ("Apple", "AAPL"),
        ("Nvidia", "NVDA"),
        ("Salesforce", "CRM"),
    ],
    "industrials_energy_transport": [
        ("Boeing", "BA"),
        ("ExxonMobil", "XOM"),
        ("Shell", "SHEL"),
        ("Chevron", "CVX"),
        ("Berkshire Hathaway Class B", "BRK-B"),
        ("Uber", "UBER"),
        ("General Electric", "GE"),
        ("RTX Corporation", "RTX"),
    ],
    "financials": [
        ("Blackstone", "BX"),
        ("KKR", "KKR"),
        ("Apollo Global Management", "APO"),
        ("BlackRock", "BLK"),
        ("Goldman Sachs", "GS"),
        ("Morgan Stanley", "MS"),
        ("Charles Schwab", "SCHW"),
        ("Chubb", "CB"),
        ("Wells Fargo", "WFC"),
        ("JPMorgan Chase", "JPM"),
        ("American Express", "AXP"),
        ("Visa", "V"),
        ("Mastercard", "MA"),
    ],
    "healthcare": [
        ("UnitedHealth", "UNH"),
        ("Bristol-Myers Squibb", "BMY"),
        ("Eli Lilly", "LLY"),
    ],
    "real_estate": [
        ("CBRE", "CBRE"),
        ("Zillow Group", "Z"),
    ],
}

# 2YY=F is the CBOE 2-Year Treasury Yield futures; may have limited history.
# ^GDBR10 and ^GUKG10 are included speculatively — flagged in errors if unavailable.
FIXED_INCOME = [
    ("US 2Y Treasury", "2YY=F"),
    ("US 10Y Treasury", "^TNX"),
    ("US 30Y Treasury", "^TYX"),
    ("German 10Y Bund", "^GDBR10"),
    ("UK 10Y Gilt", "^GUKG10"),
]

COMMODITIES = [
    ("Brent Crude", "BZ=F"),
    ("WTI Crude", "CL=F"),
    ("Gold", "GC=F"),
    ("Silver", "SI=F"),
    ("Copper", "HG=F"),
    ("Natural Gas", "NG=F"),
]

FX = [
    ("Dollar Index", "DX-Y.NYB"),
    ("EUR/USD", "EURUSD=X"),
    ("GBP/USD", "GBPUSD=X"),
    ("USD/JPY", "JPY=X"),
    ("USD/CHF", "CHF=X"),
    ("USD/ZAR", "ZAR=X"),
]

FRED_SERIES = [
    ("CPIAUCSL", "US CPI Index"),
    ("CPILFESL", "US Core CPI Index"),
    ("UNRATE", "US Unemployment Rate"),
    ("PAYEMS", "US Nonfarm Payrolls"),
    ("FEDFUNDS", "Fed Funds Rate"),
    ("BAMLH0A0HYM2", "ICE BofA US High Yield OAS"),
    ("BAMLC0A0CM", "ICE BofA US Corporate IG OAS"),
]

# ── yfinance helpers ───────────────────────────────────────────────────────────

def _year_start() -> str:
    return f"{date.today().year}-01-01"


def _tz_naive(s: pd.Series) -> pd.Series:
    """Strip timezone from DatetimeIndex so comparisons don't explode."""
    if s.index.tz is not None:
        return s.tz_localize(None)
    return s


def download_close_prices(tickers: list[str], start: str) -> dict[str, pd.Series]:
    """
    Batch-download daily adjusted close prices for all tickers in one request.
    Returns {ticker: Series}; missing tickers are simply absent from the dict.
    """
    if not tickers:
        return {}

    try:
        raw = yf.download(
            tickers,
            start=start,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception:
        return {}

    if raw.empty:
        return {}

    result: dict[str, pd.Series] = {}
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            close_df = raw["Close"]
            if isinstance(close_df, pd.Series):
                # Edge case: MultiIndex but only one ticker returned data
                result[tickers[0]] = close_df.dropna()
            else:
                for t in tickers:
                    if t in close_df.columns:
                        s = close_df[t].dropna()
                        if not s.empty:
                            result[t] = s
        elif "Close" in raw.columns:
            # Flat columns — single ticker
            result[tickers[0]] = raw["Close"].dropna()
    except Exception:
        pass

    return result


def equity_record(
    name: str, ticker: str, series: Optional[pd.Series]
) -> tuple[Optional[dict], Optional[str]]:
    """Build a standard equity/commodity/FX record from a close-price series."""
    if series is None or series.empty:
        return None, f"{ticker}: no data returned"

    s = _tz_naive(series.dropna())
    if len(s) < 2:
        return None, f"{ticker}: insufficient history ({len(s)} data points)"

    last = round(float(s.iloc[-1]), 2)
    prev_close = float(s.iloc[-2])

    change_1d = round((last - prev_close) / prev_close * 100, 2) if prev_close else None

    change_1w: Optional[float] = None
    if len(s) >= 6:
        base = float(s.iloc[-6])
        if base:
            change_1w = round((last - base) / base * 100, 2)

    change_ytd: Optional[float] = None
    year_start_ts = pd.Timestamp(_year_start())
    s_ytd = s[s.index >= year_start_ts]
    if not s_ytd.empty:
        base_ytd = float(s_ytd.iloc[0])
        if base_ytd:
            change_ytd = round((last - base_ytd) / base_ytd * 100, 2)

    return {
        "name": name,
        "ticker": ticker,
        "last": last,
        "change_1d_pct": change_1d,
        "change_1w_pct": change_1w,
        "change_ytd_pct": change_ytd,
    }, None


def fi_record(
    name: str, ticker: str, series: Optional[pd.Series]
) -> tuple[Optional[dict], Optional[str]]:
    """Build a fixed-income record: yield in %, changes in basis points."""
    if series is None or series.empty:
        return None, f"{ticker}: no data returned"

    s = _tz_naive(series.dropna())
    if len(s) < 2:
        return None, f"{ticker}: insufficient history ({len(s)} data points)"

    last_yield = round(float(s.iloc[-1]), 2)
    change_1d_bps = round((last_yield - float(s.iloc[-2])) * 100, 1)

    change_1w_bps: Optional[float] = None
    if len(s) >= 6:
        change_1w_bps = round((last_yield - float(s.iloc[-6])) * 100, 1)

    return {
        "name": name,
        "ticker": ticker,
        "last_yield_pct": last_yield,
        "change_1d_bps": change_1d_bps,
        "change_1w_bps": change_1w_bps,
    }, None


# ── FRED helper ────────────────────────────────────────────────────────────────

def fetch_fred_macro() -> tuple[list[dict], list[str]]:
    results: list[dict] = []
    errors: list[str] = []

    fred_key = os.getenv("FRED_API_KEY", "").strip()
    if not fred_key:
        return results, [
            f"FRED {sid}: FRED_API_KEY not set — skipped" for sid, _ in FRED_SERIES
        ]

    try:
        from fredapi import Fred
    except ImportError:
        return results, [
            f"FRED {sid}: fredapi not installed — skipped" for sid, _ in FRED_SERIES
        ]

    fred = Fred(api_key=fred_key)
    for series_id, name in FRED_SERIES:
        try:
            data = fred.get_series(series_id).dropna()
            if len(data) < 2:
                errors.append(f"FRED {series_id}: fewer than 2 observations available")
                continue
            results.append({
                "series_id": series_id,
                "name": name,
                "latest_value": round(float(data.iloc[-1]), 4),
                "latest_date": data.index[-1].date().isoformat(),
                "prior_value": round(float(data.iloc[-2]), 4),
                "prior_date": data.index[-2].date().isoformat(),
            })
        except Exception as exc:
            errors.append(f"FRED {series_id}: {exc}")

    return results, errors


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    errors: list[str] = []
    total_attempted = 0
    total_success = 0

    run_timestamp = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Collect all price tickers; deduplicate while preserving order.
    equity_tickers = (
        [t for _, t in INDICES]
        + [t for _, t in US_SECTORS]
        + [t for group in WATCHLIST.values() for _, t in group]
    )
    all_tickers = list(
        dict.fromkeys(
            equity_tickers
            + [t for _, t in FIXED_INCOME]
            + [t for _, t in COMMODITIES]
            + [t for _, t in FX]
        )
    )

    prices = download_close_prices(all_tickers, _year_start())

    # Canonical market date from S&P 500; fall back to latest available.
    data_as_of: str
    sp500 = prices.get("^GSPC")
    if sp500 is not None and not sp500.empty:
        data_as_of = _tz_naive(sp500).index[-1].date().isoformat()
    else:
        best: Optional[str] = None
        for s in prices.values():
            d = _tz_naive(s).index[-1].date().isoformat()
            if best is None or d > best:
                best = d
        data_as_of = best or date.today().isoformat()

    # --- process helpers ---

    def process_equity(items: list[tuple[str, str]]) -> list[dict]:
        nonlocal total_attempted, total_success
        out = []
        for name, ticker in items:
            total_attempted += 1
            rec, err = equity_record(name, ticker, prices.get(ticker))
            if err:
                errors.append(err)
            else:
                out.append(rec)
                total_success += 1
        return out

    def process_fi(items: list[tuple[str, str]]) -> list[dict]:
        nonlocal total_attempted, total_success
        out = []
        for name, ticker in items:
            total_attempted += 1
            rec, err = fi_record(name, ticker, prices.get(ticker))
            if err:
                errors.append(err)
            else:
                out.append(rec)
                total_success += 1
        return out

    # --- assemble output ---

    indices_out = process_equity(INDICES)
    sectors_out = process_equity(US_SECTORS)
    watchlist_out = {grp: process_equity(items) for grp, items in WATCHLIST.items()}
    fi_out = process_fi(FIXED_INCOME)
    commodities_out = process_equity(COMMODITIES)
    fx_out = process_equity(FX)

    macro_out, fred_errors = fetch_fred_macro()
    errors.extend(fred_errors)
    total_attempted += len(FRED_SERIES)
    total_success += len(macro_out)

    output = {
        "run_timestamp": run_timestamp,
        "data_as_of": data_as_of,
        "equities": {
            "indices": indices_out,
            "us_sectors": sectors_out,
            "watchlist": watchlist_out,
        },
        "fixed_income": fi_out,
        "commodities": commodities_out,
        "fx": fx_out,
        "macro": macro_out,
        "errors": errors,
    }

    print(json.dumps(output, indent=2))
    print(
        f"Pulled {total_success}/{total_attempted} data points successfully. {len(errors)} errors.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
