#!/usr/bin/env python3
"""
digest_backend.py — Daily market intelligence data layer.

Pipeline 1 (market data): structured close prices + FRED macro via yfinance / fredapi.
Pipeline 2 (context): RSS headline fetch + Claude Haiku classification.

Usage:
  python digest_backend.py               # run both pipelines
  python digest_backend.py --data-only   # market data only (no Anthropic API required)
  python digest_backend.py --context-only  # context only (no yfinance / FRED required)
"""

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Optional
from zoneinfo import ZoneInfo

# Fix SSL certificate verification on macOS when the system bundle is missing.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

import feedparser
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

load_dotenv(override=True)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG — all tickers, series IDs, feed URLs, and model constants live here.
# ══════════════════════════════════════════════════════════════════════════════

# ── Equity indices ─────────────────────────────────────────────────────────────

INDICES: list[tuple[str, str]] = [
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

# ── US sector ETFs ─────────────────────────────────────────────────────────────

US_SECTORS: list[tuple[str, str]] = [
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

# ── Watchlist (single stocks) ──────────────────────────────────────────────────

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
        ("Caterpillar", "CAT"),
        ("Deere", "DE"),
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
        ("Rocket Companies", "RKT"),
    ],
}

# ── Fixed income ───────────────────────────────────────────────────────────────
# 2YY=F: CBOE 2-Year Treasury Yield futures — may have gaps; gracefully skipped if unavailable.
# ^GDBR10 / ^GUKG10: not reliably on Yahoo Finance — skipped if unavailable.

FIXED_INCOME: list[tuple[str, str]] = [
    ("US 2Y Treasury", "2YY=F"),
    ("US 10Y Treasury", "^TNX"),
    ("US 30Y Treasury", "^TYX"),
    ("German 10Y Bund", "^GDBR10"),
    ("UK 10Y Gilt", "^GUKG10"),
]

# ── Commodities ────────────────────────────────────────────────────────────────

COMMODITIES: list[tuple[str, str]] = [
    ("Brent Crude", "BZ=F"),
    ("WTI Crude", "CL=F"),
    ("Gold", "GC=F"),
    ("Silver", "SI=F"),
    ("Copper", "HG=F"),
    ("Natural Gas", "NG=F"),
]

# ── FX ─────────────────────────────────────────────────────────────────────────

FX: list[tuple[str, str]] = [
    ("Dollar Index", "DX-Y.NYB"),
    ("EUR/USD", "EURUSD=X"),
    ("GBP/USD", "GBPUSD=X"),
    ("AUD/USD", "AUDUSD=X"),
    ("USD/JPY", "JPY=X"),
    ("USD/CHF", "CHF=X"),
    ("USD/ZAR", "ZAR=X"),
]

# ── FRED macro series ──────────────────────────────────────────────────────────

FRED_SERIES: list[tuple[str, str]] = [
    ("CPIAUCSL", "US CPI Index"),
    ("CPILFESL", "US Core CPI Index"),
    ("UNRATE", "US Unemployment Rate"),
    ("PAYEMS", "US Nonfarm Payrolls"),
    ("FEDFUNDS", "Fed Funds Rate"),
    ("BAMLH0A0HYM2", "ICE BofA US High Yield OAS"),
    ("BAMLC0A0CM", "ICE BofA US Corporate IG OAS"),
]

# ── RSS feed URLs ──────────────────────────────────────────────────────────────

RSS_FEEDS: list[tuple[str, str]] = [
    ("CNBC Top News",             "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC US Markets",           "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
    ("CNBC Earnings",             "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135"),
    ("CNBC Economy",              "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("MarketWatch Top Stories",   "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MarketWatch Real-time",     "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
    ("MarketWatch Market Pulse",  "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    # Reuters + Bloomberg via Google News site-scoped search — Reuters killed
    # their native RSS in 2023, and Bloomberg has no public RSS. Google News
    # surfaces the articles within a 24h window.
    ("Reuters (via GNews)",       "https://news.google.com/rss/search?q=when:24h+site:reuters.com+business&hl=en-US&gl=US&ceid=US:en"),
    ("Bloomberg (via GNews)",     "https://news.google.com/rss/search?q=when:24h+site:bloomberg.com&hl=en-US&gl=US&ceid=US:en"),
    # Seeking Alpha — analyst views, earnings previews, buy-side commentary
    ("Seeking Alpha Market",      "https://seekingalpha.com/market_currents.xml"),
    ("Seeking Alpha General",     "https://seekingalpha.com/feed.xml"),
    ("Seeking Alpha Popular",     "https://seekingalpha.com/listing/most-popular-articles.xml"),
    # Central banks — direct from the source for monetary policy signals
    ("Federal Reserve",           "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("ECB Press",                 "https://www.ecb.europa.eu/rss/press.html"),
    ("Bank of England",           "https://www.bankofengland.co.uk/rss/news"),
    ("Bank of Japan",             "https://www.boj.or.jp/en/rss/whatsnew.xml"),
    # European + Asian institutional coverage
    ("FT Markets (via GNews)",    "https://news.google.com/rss/search?q=when:24h+site:ft.com+markets&hl=en-US&gl=US&ceid=US:en"),
    ("Nikkei Asia Business",      "https://asia.nikkei.com/rss/feed/nar"),
    ("China econ (via GNews)",    "https://news.google.com/rss/search?q=when:24h+%28China+OR+PBoC+OR+Beijing%29+economy&hl=en-US&gl=US&ceid=US:en"),
    ("Japan econ (via GNews)",    "https://news.google.com/rss/search?q=when:24h+%28Japan+OR+BoJ+OR+Tokyo%29+%28economy+OR+markets%29&hl=en-US&gl=US&ceid=US:en"),
]

# Yahoo Finance has a primary + fallback URL. Tried in order; first non-empty wins.
YAHOO_FINANCE_FEED_URLS: list[str] = [
    "https://finance.yahoo.com/news/rssindex",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
]

# Per-ticker Yahoo Finance feeds. Pulling these in addition to the general
# feeds ensures watchlist-specific coverage (analyst notes, earnings previews,
# management commentary) isn't crowded out by macro headlines.
YAHOO_TICKER_FEED_TMPL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"

# ── Classifier settings ────────────────────────────────────────────────────────

CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
CLASSIFIER_BATCH_SIZE = 12
TOP_N_PER_CLASS = 5
# Override via env (DIGEST_CONTEXT_WINDOW_HOURS=120 for the Friday weekly wrap).
CONTEXT_WINDOW_HOURS = int(os.environ.get("DIGEST_CONTEXT_WINDOW_HOURS", "24"))
ASSET_CLASSES = ["equities", "fixed_income", "commodities", "fx", "macro"]

# Claude Haiku 4.5 pricing (USD per token)
HAIKU_INPUT_PRICE_PER_TOK  = 0.80 / 1_000_000
HAIKU_OUTPUT_PRICE_PER_TOK = 4.00 / 1_000_000


# ══════════════════════════════════════════════════════════════════════════════
# SHARED UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def safe_fetch(url: str, timeout: int = 15) -> tuple[Optional[bytes], Optional[str]]:
    """
    Fetch a URL. Returns (content_bytes, None) on success or (None, error_str) on failure.
    Used by both pipelines for any HTTP request.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DigestBackend/1.0 (financial news aggregator)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} fetching {url}"
    except urllib.error.URLError as exc:
        return None, f"URL error fetching {url}: {exc.reason}"
    except Exception as exc:
        return None, f"Error fetching {url}: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DATA PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def _year_start() -> str:
    return f"{date.today().year}-01-01"


def _tz_naive(s: pd.Series) -> pd.Series:
    if s.index.tz is not None:
        return s.tz_localize(None)
    return s


def _series_to_history(s: pd.Series, max_points: int = 90) -> list[list]:
    """Format a price/yield series as [[date_iso, value], ...] for frontend
    sparkline rendering. Keeps the last `max_points` trading days (default
    90 ≈ one quarter, enough visual context for 1d/1w sparklines and a
    reasonable proxy for YTD by mid-year). Rounded to 4dp for compactness.
    """
    if s is None or s.empty:
        return []
    s = _tz_naive(s.dropna()).tail(max_points)
    return [[idx.date().isoformat(), round(float(val), 4)] for idx, val in s.items()]


def _download_close_prices(tickers: list[str], start: str) -> dict[str, pd.Series]:
    """Batch-download daily adjusted close prices. Returns {ticker: Series}."""
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
                result[tickers[0]] = close_df.dropna()
            else:
                for t in tickers:
                    if t in close_df.columns:
                        s = close_df[t].dropna()
                        if not s.empty:
                            result[t] = s
        elif "Close" in raw.columns:
            result[tickers[0]] = raw["Close"].dropna()
    except Exception:
        pass

    return result


def _equity_record(
    name: str, ticker: str, series: Optional[pd.Series]
) -> tuple[Optional[dict], Optional[str]]:
    if series is None or series.empty:
        return None, f"{ticker}: no data returned"
    s = _tz_naive(series.dropna())
    if len(s) < 2:
        return None, f"{ticker}: insufficient history ({len(s)} data points)"

    last = round(float(s.iloc[-1]), 2)
    prev = float(s.iloc[-2])
    change_1d = round((last - prev) / prev * 100, 2) if prev else None

    change_1w: Optional[float] = None
    if len(s) >= 6:
        base = float(s.iloc[-6])
        if base:
            change_1w = round((last - base) / base * 100, 2)

    change_ytd: Optional[float] = None
    year_ts = pd.Timestamp(_year_start())
    s_ytd = s[s.index >= year_ts]
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
        "history": _series_to_history(s),
    }, None


def _fi_record(
    name: str, ticker: str, series: Optional[pd.Series]
) -> tuple[Optional[dict], Optional[str]]:
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
        "history": _series_to_history(s),
    }, None


def _fetch_fred_macro() -> tuple[list[dict], list[str]]:
    results: list[dict] = []
    errors: list[str] = []
    fred_key = os.getenv("FRED_API_KEY", "").strip()
    if not fred_key:
        return results, [f"FRED {sid}: FRED_API_KEY not set — skipped" for sid, _ in FRED_SERIES]
    try:
        from fredapi import Fred
    except ImportError:
        return results, [f"FRED {sid}: fredapi not installed — skipped" for sid, _ in FRED_SERIES]

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


def run_market_data_pipeline() -> tuple[dict, list[str], int, int]:
    """
    Run the structured market data pipeline.
    Returns (data_dict, errors, total_attempted, total_success).
    data_dict keys: data_as_of, equities, fixed_income, commodities, fx, macro.
    """
    errors: list[str] = []
    total_attempted = 0
    total_success = 0

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

    prices = _download_close_prices(all_tickers, _year_start())

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

    def process_equity(items: list[tuple[str, str]]) -> list[dict]:
        nonlocal total_attempted, total_success
        out = []
        for name, ticker in items:
            total_attempted += 1
            rec, err = _equity_record(name, ticker, prices.get(ticker))
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
            rec, err = _fi_record(name, ticker, prices.get(ticker))
            if err:
                errors.append(err)
            else:
                out.append(rec)
                total_success += 1
        return out

    indices_out    = process_equity(INDICES)
    sectors_out    = process_equity(US_SECTORS)
    watchlist_out  = {grp: process_equity(items) for grp, items in WATCHLIST.items()}
    fi_out         = process_fi(FIXED_INCOME)
    commodities_out = process_equity(COMMODITIES)
    fx_out         = process_equity(FX)

    macro_out, fred_errors = _fetch_fred_macro()
    errors.extend(fred_errors)
    total_attempted += len(FRED_SERIES)
    total_success   += len(macro_out)

    data = {
        "data_as_of": data_as_of,
        "equities": {
            "indices":    indices_out,
            "us_sectors": sectors_out,
            "watchlist":  watchlist_out,
        },
        "fixed_income": fi_out,
        "commodities":  commodities_out,
        "fx":           fx_out,
        "macro":        macro_out,
    }
    return data, errors, total_attempted, total_success


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(" ".join(self._parts).split())


def _strip_html(text: str) -> str:
    try:
        s = _HTMLStripper()
        s.feed(text or "")
        return s.get_text()
    except Exception:
        return re.sub(r"<[^>]+>", "", text or "").strip()


def _clean_description(text: str, max_chars: int = 300) -> str:
    stripped = _strip_html(text)
    return stripped[:max_chars].rstrip() if len(stripped) > max_chars else stripped


def _parse_pub_time(entry) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def _fetch_feeds(errors: list[str]) -> tuple[list[dict], int, int]:
    """
    Fetch all RSS feeds. Returns (raw_items, feeds_attempted, feeds_ok).
    Each item dict has: title, description_raw, url, source, published_dt.
    """
    raw_items: list[dict] = []
    feeds_attempted = 0
    feeds_ok = 0

    is_google_news = lambda name: "GNews" in name  # titles have " - Publisher" suffix

    def _parse_feed(source_name: str, url: str) -> list[dict]:
        nonlocal feeds_attempted, feeds_ok
        feeds_attempted += 1
        content, err = safe_fetch(url)
        if err:
            errors.append(f"Feed '{source_name}': {err}")
            return []
        feed = feedparser.parse(content)
        if not feed.entries:
            errors.append(f"Feed '{source_name}': returned 0 entries ({url})")
            return []
        feeds_ok += 1
        strip_suffix = is_google_news(source_name)
        items = []
        for entry in feed.entries:
            title = getattr(entry, "title", "")
            # Google News format: "Headline text - Publisher" — strip the publisher suffix.
            if strip_suffix:
                title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()
            items.append({
                "title":           title,
                "description_raw": getattr(entry, "summary", getattr(entry, "description", "")),
                "url":             getattr(entry, "link", ""),
                "source":          source_name,
                "published_dt":    _parse_pub_time(entry),
            })
        return items

    # Fixed feeds
    for source_name, url in RSS_FEEDS:
        raw_items.extend(_parse_feed(source_name, url))

    # Yahoo Finance: try primary, fall back if empty
    yahoo_added = False
    for yf_url in YAHOO_FINANCE_FEED_URLS:
        if yahoo_added:
            break
        feeds_attempted += 1
        content, err = safe_fetch(yf_url)
        if err:
            errors.append(f"Feed 'Yahoo Finance' ({yf_url}): {err}")
            continue
        feed = feedparser.parse(content)
        if feed.entries:
            feeds_ok += 1
            for entry in feed.entries:
                raw_items.append({
                    "title":           getattr(entry, "title", ""),
                    "description_raw": getattr(entry, "summary", getattr(entry, "description", "")),
                    "url":             getattr(entry, "link", ""),
                    "source":          "Yahoo Finance",
                    "published_dt":    _parse_pub_time(entry),
                })
            yahoo_added = True
        else:
            errors.append(f"Feed 'Yahoo Finance' ({yf_url}): returned 0 entries")

    if not yahoo_added:
        errors.append("Yahoo Finance: all feed URLs failed or returned 0 entries — skipped")

    # Per-ticker Yahoo feeds: one per watchlist company. Most ticker feeds
    # return a handful of items; some fail quietly (empty feed) for less
    # active names or tickers with dashes. _parse_feed increments the
    # feeds_attempted/feeds_ok counters internally; we just consume the items.
    for _, ticker in [(n, t) for group in WATCHLIST.values() for n, t in group]:
        url = YAHOO_TICKER_FEED_TMPL.format(ticker=ticker)
        items = _parse_feed(f"Yahoo Finance [{ticker}]", url)
        # Tag each item with its ticker so downstream can treat per-ticker
        # items as guaranteed watchlist mentions without re-scanning text.
        for item in items:
            item["_watchlist_ticker"] = ticker
        raw_items.extend(items)

    return raw_items, feeds_attempted, feeds_ok


_CLASSIFY_SYSTEM = """You are a financial news classifier. For each numbered item, classify it by market-relevant asset class and assign a relevance score.

Return a JSON array with one object per input item (same order):
[{"tags": [...], "relevance": N}, ...]

Asset class tags — include all that apply:
  equities       stock indices, sectors, single-stock market moves, earnings-driven price action
  fixed_income   Treasury yields, credit spreads, central bank commentary, bond dynamics
  commodities    oil, gold, silver, copper, natural gas, agricultural commodities
  fx             USD, EUR, JPY, CHF, ZAR, GBP, other major currency moves
  macro          CPI, payrolls, retail sales, GDP, PMI, central bank policy, economic releases

Relevance (integer 1–5):
  5  Clearly explains why a market moved ("yields rose as traders priced in hawkish Fed minutes")
  4  Describes a market move with solid context
  3  Mentions a market move, even if briefly
  2  Financial news but no market-driver context
  1  No market relevance at all

For items with no market relevance use {"tags": [], "relevance": 1}.
Output only the JSON array — no explanation, no markdown."""


def _parse_classifier_response(text: str, n: int) -> list[dict]:
    """Parse the JSON array from a classifier response. Returns n items (may pad with nulls)."""
    stripped = text.strip()
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
        stripped = stripped.strip()
    try:
        result = json.loads(stripped)
        if isinstance(result, list):
            return (result + [{"tags": [], "relevance": 0}] * n)[:n]
    except json.JSONDecodeError:
        pass
    # Fallback: extract outermost JSON array (greedy, spans nested brackets).
    match = re.search(r"\[.*\]", stripped, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                return (result + [{"tags": [], "relevance": 0}] * n)[:n]
        except json.JSONDecodeError:
            pass
    return [{"tags": [], "relevance": 0}] * n


def _classify_batch(
    client,
    batch: list[dict],
    errors: list[str],
) -> tuple[list[dict], int, int]:
    """
    Classify a batch of news items. Returns (classifications, input_tokens, output_tokens).
    classifications[i] = {"tags": [...], "relevance": N}
    """
    lines = []
    for i, item in enumerate(batch, 1):
        desc = _clean_description(item["description_raw"], max_chars=200)
        lines.append(
            f"{i}. [{item['source']}]\n"
            f"   Headline: {item['title']}\n"
            f"   Description: {desc}"
        )
    user_msg = "Classify these news items:\n\n" + "\n\n".join(lines)

    try:
        resp = client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=512,
            system=_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text if resp.content else "[]"
        in_toks  = resp.usage.input_tokens  if resp.usage else 0
        out_toks = resp.usage.output_tokens if resp.usage else 0
        return _parse_classifier_response(text, len(batch)), in_toks, out_toks
    except Exception as exc:
        errors.append(f"Classifier batch failed: {exc}")
        return [{"tags": [], "relevance": 0}] * len(batch), 0, 0


def _select_top_items(classified: list[dict]) -> dict[str, list[dict]]:
    """
    Group items by asset class and select top N by relevance (most recent as tiebreak).
    An item with multiple tags appears in each relevant class's list.

    Watchlist-mention bypass: within the `equities` class, any item that came
    from a per-ticker Yahoo feed is admitted unconditionally (in addition to
    the top N). This guarantees coverage of watchlist names on days when
    macro headlines would otherwise crowd them out of the top-N cut.
    """
    by_class: dict[str, list[dict]] = {cls: [] for cls in ASSET_CLASSES}
    for item in classified:
        for tag in item.get("tags", []):
            if tag in by_class:
                by_class[tag].append(item)

    result = {}
    for cls, items in by_class.items():
        sorted_items = sorted(
            items,
            key=lambda x: (x["relevance"], x.get("published", "")),
            reverse=True,
        )
        selected = list(sorted_items[:TOP_N_PER_CLASS])

        if cls == "equities":
            seen_urls = {x.get("url") for x in selected}
            bypass = [
                x for x in sorted_items
                if x.get("_watchlist_ticker") and x.get("url") not in seen_urls
            ]
            selected.extend(bypass)

        result[cls] = [
            {
                "headline":    x["title"],
                "description": _clean_description(x["description_raw"]),
                "source":      x["source"],
                "url":         x["url"],
                "tags":        x["tags"],
                "relevance":   x["relevance"],
                "published":   x.get("published", ""),
                "ticker":      x.get("_watchlist_ticker"),
            }
            for x in selected
        ]
    return result


def run_context_pipeline() -> tuple[Optional[dict], list[str]]:
    """
    Run the RSS context pipeline.
    Returns (context_dict, errors).
    context_dict keys: window_hours, by_asset_class, stats.
    Returns (None, errors) if the pipeline cannot start (missing API key, etc.).
    """
    errors: list[str] = []

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        errors.append("Context pipeline: ANTHROPIC_API_KEY not set — skipped")
        return None, errors

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        errors.append("Context pipeline: anthropic package not installed")
        return None, errors

    stats: dict = {
        "feeds_attempted": 0,
        "feeds_ok": 0,
        "items_fetched": 0,
        "items_classified": 0,
        "items_selected": 0,
        "api_calls": 0,
        "estimated_cost_usd": 0.0,
    }

    # 1. Fetch all feeds
    raw_items, stats["feeds_attempted"], stats["feeds_ok"] = _fetch_feeds(errors)

    # 2. Filter to window
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CONTEXT_WINDOW_HOURS)
    windowed = [item for item in raw_items if item["published_dt"] and item["published_dt"] >= cutoff]

    # 3. Dedup by URL (keep order; preserve multi-outlet coverage of the same story)
    seen_urls: set[str] = set()
    deduped: list[dict] = []
    for item in windowed:
        url = item.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(item)
        elif not url:
            deduped.append(item)  # no URL — keep it

    stats["items_fetched"] = len(deduped)

    # Attach ISO published string for sorting later
    for item in deduped:
        dt = item.get("published_dt")
        item["published"] = dt.isoformat() if dt else ""

    if not deduped:
        by_class = {cls: [] for cls in ASSET_CLASSES}
        return {"window_hours": CONTEXT_WINDOW_HOURS, "by_asset_class": by_class, "stats": stats}, errors

    # 4. Classify in batches
    total_in_toks = 0
    total_out_toks = 0
    classified: list[dict] = []

    for i in range(0, len(deduped), CLASSIFIER_BATCH_SIZE):
        batch = deduped[i : i + CLASSIFIER_BATCH_SIZE]
        results, in_toks, out_toks = _classify_batch(client, batch, errors)
        total_in_toks  += in_toks
        total_out_toks += out_toks
        stats["api_calls"] += 1
        stats["items_classified"] += len(batch)
        for item, clf in zip(batch, results):
            item["tags"]      = clf.get("tags", [])
            item["relevance"] = clf.get("relevance", 0)
            classified.append(item)

    cost = total_in_toks * HAIKU_INPUT_PRICE_PER_TOK + total_out_toks * HAIKU_OUTPUT_PRICE_PER_TOK
    stats["estimated_cost_usd"] = round(cost, 6)

    # 5. Filter: drop relevance ≤ 2 or no tags
    relevant = [item for item in classified if item["relevance"] >= 3 and item.get("tags")]

    # 6. Select top N per asset class
    by_class = _select_top_items(relevant)
    stats["items_selected"] = sum(len(v) for v in by_class.values())

    return {
        "window_hours":   CONTEXT_WINDOW_HOURS,
        "by_asset_class": by_class,
        "stats":          stats,
    }, errors


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Digest backend — market data + context pipelines."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--data-only",    action="store_true", help="Run market data pipeline only")
    group.add_argument("--context-only", action="store_true", help="Run context pipeline only")
    args = parser.parse_args()

    run_timestamp = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_errors: list[str] = []

    market_data_out: Optional[dict] = None
    context_out: Optional[dict] = None
    md_summary = ""
    ctx_summary = ""

    # ── Market data pipeline ───────────────────────────────────────────────────
    if not args.context_only:
        try:
            md_data, md_errors, attempted, success = run_market_data_pipeline()
            market_data_out = md_data
            all_errors.extend(md_errors)
            md_summary = f"Market data: pulled {success}/{attempted} data points, {len(md_errors)} errors."
        except Exception as exc:
            all_errors.append(f"Market data pipeline crashed: {exc}")
            md_summary = f"Market data: CRASHED — {exc}"
    else:
        md_summary = "Market data: skipped (--context-only)."

    # ── Context pipeline ───────────────────────────────────────────────────────
    if not args.data_only:
        try:
            ctx_data, ctx_errors = run_context_pipeline()
            context_out = ctx_data
            all_errors.extend(ctx_errors)
            if ctx_data:
                s = ctx_data["stats"]
                ctx_summary = (
                    f"Context: classified {s['items_classified']} items, "
                    f"{s['items_selected']} selected across {len(ASSET_CLASSES)} asset classes, "
                    f"{len(ctx_errors)} errors, estimated cost ${s['estimated_cost_usd']:.4f}."
                )
            else:
                ctx_summary = f"Context: no output — {len(ctx_errors)} errors."
        except Exception as exc:
            all_errors.append(f"Context pipeline crashed: {exc}")
            ctx_summary = f"Context: CRASHED — {exc}"
    else:
        ctx_summary = "Context: skipped (--data-only)."

    output = {
        "run_timestamp": run_timestamp,
        "market_data":   market_data_out,
        "context":       context_out,
        "errors":        all_errors,
    }

    print(json.dumps(output, indent=2))
    print(md_summary,  file=sys.stderr)
    print(ctx_summary, file=sys.stderr)


if __name__ == "__main__":
    main()
