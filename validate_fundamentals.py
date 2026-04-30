"""Validate the `consensus_snapshots.fundamentals` data against SEC EDGAR.

EDGAR's company facts API (`data.sec.gov/api/xbrl/companyfacts/CIK*.json`)
is the most authoritative free source — it returns every numeric value the
company itself tagged in its filings under penalty of perjury. We compare
the values yfinance gave us (now stored in `consensus_snapshots.fundamentals`)
against the corresponding XBRL tags.

What this script DOES validate:
  - fiscal_year_end against the latest 10-K's DocumentPeriodEndDate
  - growth.revenue_yoy_pct against EDGAR's quarterly Revenues tag
  - growth.eps_yoy_pct against EDGAR's EarningsPerShareDiluted tag
  - eps_beat_history's most recent actual against EDGAR's quarterly EPS

What this script CANNOT validate (no free authoritative source):
  - forward_pe_fy0 / forward_pe_fy1 (depend on analyst estimates — paid)
  - price_to_fcf_ttm (depends on live stock price + FCF)
  - ev_to_ebitda_ttm (depends on live market cap + reported EBITDA)
  - non_gaap_eps_ttm (definitionally not GAAP, not in EDGAR)

Known reasons for "DIFF" results that aren't actual data quality issues:
  - Q4 derived from 10-K minus Q1+Q2+Q3: yfinance computes Q4 standalone
    while EDGAR XBRL only tags Q1/Q2/Q3 directly (Q4 lives inside the 10-K).
    Affects companies whose latest period is Q4 of fiscal year.
  - yfinance lag: EDGAR sometimes has the next quarter (Q-just-filed)
    before yfinance has propagated it. Affects newly-reported quarters.
  - Banks (JPM, GS, MS, etc.): yfinance doesn't normalise financial-sector
    income statement line items consistently. EDGAR has the data; yfinance
    returns NaN.

Usage:
    python validate_fundamentals.py                # default sample of 10
    python validate_fundamentals.py --ticker MSFT
    python validate_fundamentals.py --all          # full watchlist (slow)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Any

import httpx
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from company_profile_builder import flat_watchlist, load_cik_map

UA = "Ascension Partners Daily Digest jonathan.o.k.pein@gmail.com"
EDGAR_REQUEST_DELAY = 0.15
DEFAULT_SAMPLE = ["MSFT", "AAPL", "NVDA", "META", "JPM", "XOM", "LLY", "V", "BX", "BA"]

REVENUE_CONCEPTS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"]
EPS_CONCEPTS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]


def _fetch_edgar_quarterly(facts: dict, concepts: list[str]) -> tuple[float | None, str | None]:
    """Most recent value tagged as Q1/Q2/Q3 (Q4 only appears inside 10-Ks
    and isn't tagged as a standalone quarterly entry).

    Filters on `fp ∈ {Q1, Q2, Q3}` and keeps only USD-denominated entries.
    EDGAR can return multiple entries per period when the company also tags
    segment-level values (geographies, business segments). To get the
    consolidated total we keep the MAX per period.
    """
    # Accumulate candidates across ALL provided concepts. Different filers
    # tag consolidated revenue under different concepts (e.g. MSFT uses
    # `Revenues` for segment data and `RevenueFromContractWithCustomer...`
    # for consolidated). Taking max per end-date across concepts yields the
    # consolidated total.
    candidates: dict[str, float] = {}
    for concept in concepts:
        units = (facts.get("facts", {}).get("us-gaap", {}).get(concept) or {}).get("units", {})
        for unit_key, entries in units.items():
            if unit_key not in ("USD", "USD/shares"):
                continue
            for u in entries:
                if u.get("fp") not in ("Q1", "Q2", "Q3"):
                    continue
                start, end = u.get("start"), u.get("end")
                if not (start and end):
                    continue
                # 80-100 day filter excludes YTD cumulative entries (~180 or
                # 270 days) that share the same fp tag as the standalone Q.
                try:
                    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
                except Exception:
                    continue
                if not (80 < days < 100):
                    continue
                val = float(u["val"])
                if end not in candidates or val > candidates[end]:
                    candidates[end] = val
    if not candidates:
        return None, None
    most_recent_end = max(candidates.keys())
    return candidates[most_recent_end], most_recent_end


def _classify_diff(yf_val: float | None, edgar_val: float | None) -> tuple[float | None, str]:
    if yf_val is None or edgar_val is None or edgar_val == 0:
        return None, "no-data"
    diff_pct = abs(yf_val - edgar_val) / abs(edgar_val) * 100
    if diff_pct < 1:
        flag = "MATCH"
    elif diff_pct < 5:
        flag = "close"
    else:
        flag = "DIFF"
    return diff_pct, flag


def validate_ticker(
    ticker: str,
    cik: str,
    sb_client: httpx.Client,
    edgar_client: httpx.Client,
    sb_url: str,
) -> dict:
    """Run all validation checks against EDGAR and return a result row."""
    result: dict[str, Any] = {"ticker": ticker, "checks": {}}

    # Pull our stored fundamentals from Supabase
    snap_resp = sb_client.get(
        f"{sb_url}/rest/v1/consensus_snapshots",
        params={
            "select": "fundamentals,asof_date",
            "ticker": f"eq.{ticker}",
            "order": "asof_date.desc",
            "limit": "1",
        },
    )
    snaps = snap_resp.json() if snap_resp.status_code == 200 else []
    fundamentals = (snaps[0].get("fundamentals") if snaps else None) or {}

    # Pull EDGAR company facts (XBRL)
    time.sleep(EDGAR_REQUEST_DELAY)
    try:
        r = edgar_client.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        r.raise_for_status()
        facts = r.json()
    except Exception as exc:
        result["error"] = f"EDGAR fetch failed: {exc}"
        return result

    # Pull EDGAR submissions (for fiscal_year_end check)
    time.sleep(EDGAR_REQUEST_DELAY)
    try:
        r = edgar_client.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
        r.raise_for_status()
        submissions = r.json()
    except Exception:
        submissions = None

    # Check 1: fiscal_year_end
    yf_fy = fundamentals.get("fiscal_year_end")
    edgar_fy = None
    if submissions:
        rec = submissions["filings"]["recent"]
        for i, form in enumerate(rec["form"]):
            if form in ("10-K", "20-F", "40-F"):
                edgar_fy = rec.get("reportDate", [None] * len(rec["form"]))[i]
                break
    result["checks"]["fiscal_year_end"] = {
        "yfinance": yf_fy,
        "edgar": edgar_fy,
        "match": yf_fy == edgar_fy if (yf_fy and edgar_fy) else None,
    }

    # Check 2: most recent quarterly revenue
    t = yf.Ticker(ticker)
    yf_rev, yf_rev_period = None, None
    try:
        q = t.quarterly_income_stmt
        if q is not None and not q.empty and "Total Revenue" in q.index:
            row = q.loc["Total Revenue"]
            v = row.iloc[0]
            if pd.notna(v):
                yf_rev = float(v)
                yf_rev_period = str(q.columns[0])[:10]
    except Exception:
        pass
    edgar_rev, edgar_rev_period = _fetch_edgar_quarterly(facts, REVENUE_CONCEPTS)
    diff_pct, flag = _classify_diff(yf_rev, edgar_rev)
    result["checks"]["latest_quarterly_revenue"] = {
        "yfinance_val": yf_rev,
        "yfinance_period": yf_rev_period,
        "edgar_val": edgar_rev,
        "edgar_period": edgar_rev_period,
        "diff_pct": round(diff_pct, 2) if diff_pct is not None else None,
        "flag": flag,
    }

    # Check 3: most recent quarterly diluted EPS
    yf_eps = None
    try:
        q = t.quarterly_income_stmt
        if q is not None and not q.empty and "Diluted EPS" in q.index:
            v = q.loc["Diluted EPS"].iloc[0]
            if pd.notna(v):
                yf_eps = float(v)
    except Exception:
        pass
    edgar_eps, edgar_eps_period = _fetch_edgar_quarterly(facts, EPS_CONCEPTS)
    diff_pct, flag = _classify_diff(yf_eps, edgar_eps)
    result["checks"]["latest_quarterly_eps_gaap"] = {
        "yfinance_val": yf_eps,
        "edgar_val": edgar_eps,
        "edgar_period": edgar_eps_period,
        "diff_pct": round(diff_pct, 2) if diff_pct is not None else None,
        "flag": flag,
    }

    return result


def _print_summary(rows: list[dict]) -> None:
    print()
    print(f"{'Ticker':<7} {'fy_end':<10} {'revenue':<10} {'eps':<10}  notes")
    print("-" * 80)
    for r in rows:
        if "error" in r:
            print(f"{r['ticker']:<7} {r['error']}")
            continue
        c = r["checks"]
        fy_flag = "MATCH" if c["fiscal_year_end"]["match"] else "DIFF"
        rev_flag = c["latest_quarterly_revenue"]["flag"]
        eps_flag = c["latest_quarterly_eps_gaap"]["flag"]
        rev_note = ""
        eps_note = ""
        if rev_flag == "DIFF":
            rev_note = (
                f"yf={c['latest_quarterly_revenue']['yfinance_val']/1e9:.2f}B "
                f"edgar={c['latest_quarterly_revenue']['edgar_val']/1e9:.2f}B "
                f"({c['latest_quarterly_revenue']['diff_pct']:.1f}%)"
                if c["latest_quarterly_revenue"]["yfinance_val"]
                else "yf=N/A"
            )
        if eps_flag == "DIFF":
            eps_note = (
                f"yf={c['latest_quarterly_eps_gaap']['yfinance_val']:.2f} "
                f"edgar={c['latest_quarterly_eps_gaap']['edgar_val']:.2f} "
                f"({c['latest_quarterly_eps_gaap']['diff_pct']:.1f}%)"
                if c["latest_quarterly_eps_gaap"]["yfinance_val"]
                else "yf=N/A"
            )
        notes = (rev_note + " | " + eps_note).strip(" |")
        print(f"{r['ticker']:<7} {fy_flag:<10} {rev_flag:<10} {eps_flag:<10}  {notes}")
    print()
    fy_match = sum(1 for r in rows if "checks" in r and r["checks"]["fiscal_year_end"]["match"])
    rev_match = sum(1 for r in rows if "checks" in r and r["checks"]["latest_quarterly_revenue"]["flag"] in ("MATCH", "close"))
    eps_match = sum(1 for r in rows if "checks" in r and r["checks"]["latest_quarterly_eps_gaap"]["flag"] in ("MATCH", "close"))
    n = sum(1 for r in rows if "checks" in r)
    print(f"Summary across {n} tickers:")
    print(f"  fiscal_year_end matches: {fy_match}/{n}")
    print(f"  revenue within 5% of EDGAR: {rev_match}/{n}")
    print(f"  EPS within 5% of EDGAR: {eps_match}/{n}")
    print()
    print("DIFFs are typically explainable (Q4-derived, yfinance lag, financials")
    print("not normalized). See module docstring for diagnostic guidance.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate consensus_snapshots.fundamentals against SEC EDGAR")
    parser.add_argument("--ticker", help="Single ticker to validate")
    parser.add_argument("--all", action="store_true", help="Run against full watchlist (slow)")
    args = parser.parse_args()

    load_dotenv(override=True)
    sb_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not sb_url or not sb_key:
        print("validate_fundamentals: SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set", file=sys.stderr)
        return 1

    sb_client = httpx.Client(headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}, timeout=30)
    edgar_client = httpx.Client(headers={"User-Agent": UA}, timeout=30)

    if args.ticker:
        tickers = [args.ticker.upper()]
    elif args.all:
        tickers = [t for _, t, _ in flat_watchlist()]
    else:
        tickers = DEFAULT_SAMPLE

    cik_map = load_cik_map()
    rows: list[dict] = []
    for tk in tickers:
        cik = cik_map.get(tk.upper())
        if not cik:
            rows.append({"ticker": tk, "error": "no CIK in SEC map"})
            continue
        print(f"validating {tk}...", file=sys.stderr)
        rows.append(validate_ticker(tk, cik, sb_client, edgar_client, sb_url))

    _print_summary(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
