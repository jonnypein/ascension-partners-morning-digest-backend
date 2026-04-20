"""Discover recent earnings 8-Ks for watchlist tickers and assemble input
bundles for earnings_writer.py.

One bundle per ticker that filed an 8-K with Item 2.02 ("Results of
Operations") in the last LOOKBACK_HOURS. Each bundle contains:

    ticker, company_name, sector, fiscal_period (best-effort),
    press_release_text, consensus (rev + eps), price_reaction,
    prior_guidance, filed_at, source_urls.

Non-US filers (e.g. Shell, which files 6-K) produce zero hits and are
skipped silently. Output is JSON printed to stdout; summary to stderr.

Usage:
    python earnings_backend.py
    python earnings_backend.py --ticker JPM
    python earnings_backend.py --lookback-hours 72
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yfinance as yf
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from digest_backend import WATCHLIST

UA = "Ascension Partners Morning Digest jonathan.o.k.pein@gmail.com"
SEC_TIMEOUT = 20
YF_TIMEOUT = 20
DEFAULT_LOOKBACK_HOURS = 36
SEC_REQUEST_DELAY = 0.12  # SEC limits to 10 req/sec; stay well under
SEC_CLIENT = httpx.Client(headers={"User-Agent": UA}, timeout=SEC_TIMEOUT)


def flat_watchlist() -> list[tuple[str, str, str]]:
    """Flatten WATCHLIST into [(company_name, ticker, sector_key), ...]."""
    out = []
    for sector, items in WATCHLIST.items():
        for name, ticker in items:
            out.append((name, ticker, sector))
    return out


def load_cik_map() -> dict[str, str]:
    """Return {TICKER: zero-padded 10-digit CIK}."""
    r = SEC_CLIENT.get("https://www.sec.gov/files/company_tickers.json")
    r.raise_for_status()
    return {
        v["ticker"].upper(): str(v["cik_str"]).zfill(10)
        for v in r.json().values()
    }


def recent_earnings_filings(cik: str, cutoff_utc: datetime) -> list[dict]:
    """Return [{accessionNumber, primaryDocument, filingDate, acceptanceDateTime, items}]
    for 8-Ks with Item 2.02 filed at or after cutoff_utc."""
    time.sleep(SEC_REQUEST_DELAY)
    r = SEC_CLIENT.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    r.raise_for_status()
    recent = r.json()["filings"]["recent"]
    out = []
    for i, form in enumerate(recent["form"]):
        if form != "8-K":
            continue
        items = recent["items"][i] if i < len(recent.get("items", [])) else ""
        if "2.02" not in items:
            continue
        accepted = datetime.fromisoformat(
            recent["acceptanceDateTime"][i].replace("Z", "+00:00")
        )
        if accepted < cutoff_utc:
            continue
        out.append({
            "accessionNumber": recent["accessionNumber"][i],
            "primaryDocument": recent["primaryDocument"][i],
            "filingDate": recent["filingDate"][i],
            "acceptanceDateTime": recent["acceptanceDateTime"][i],
            "items": items,
        })
    return out


def fetch_ex_991(cik_no_zero: str, accession: str) -> tuple[str, str] | None:
    """Locate EX-99.1 in the filing index and return (filename, extracted_text).
    Returns None if no 99.1 exhibit is found."""
    acc_no_dash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zero}/{acc_no_dash}"

    time.sleep(SEC_REQUEST_DELAY)
    r = SEC_CLIENT.get(f"{base}/{accession}-index.html")
    r.raise_for_status()

    # The index has rows like: [seq, description, filename, type, size].
    # Type column values include "EX-99.1", "EX-99.2", etc.
    filename = None
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", r.text, re.S | re.I):
        cells = [
            re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(1), re.S | re.I)
        ]
        if len(cells) >= 4 and cells[3].upper() == "EX-99.1":
            filename = cells[2]
            break

    if not filename:
        return None

    time.sleep(SEC_REQUEST_DELAY)
    r = SEC_CLIENT.get(f"{base}/{filename}")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return (filename, text)


def get_consensus(ticker: str) -> dict:
    """Return {revenue_consensus, eps_consensus, revenue_detail, eps_detail}.
    Numbers are the 0q (current quarter) analyst average. Detail rows are
    all available quarterly estimates so Claude can pick the right period
    if 0q has already rolled forward."""
    out = {
        "revenue_consensus": None,
        "eps_consensus": None,
        "revenue_detail": [],
        "eps_detail": [],
    }
    try:
        t = yf.Ticker(ticker)
        rev = t.revenue_estimate
        if rev is not None and not rev.empty:
            for period, row in rev.iterrows():
                out["revenue_detail"].append({
                    "period": str(period),
                    "avg": float(row["avg"]) if row.get("avg") is not None else None,
                })
                if str(period) == "0q" and row.get("avg") is not None:
                    out["revenue_consensus"] = float(row["avg"])
        eps = t.earnings_estimate
        if eps is not None and not eps.empty:
            for period, row in eps.iterrows():
                out["eps_detail"].append({
                    "period": str(period),
                    "avg": float(row["avg"]) if row.get("avg") is not None else None,
                })
                if str(period) == "0q" and row.get("avg") is not None:
                    out["eps_consensus"] = float(row["avg"])
    except Exception as exc:
        print(f"[earnings] consensus fetch failed for {ticker}: {exc}", file=sys.stderr)
    return out


def get_price_reaction(ticker: str, announcement_date: datetime) -> dict:
    """Compute next-session close-to-close move. After-hours left null in v1."""
    out = {
        "after_hours_pct": None,
        "next_session_pct": None,
        "context": None,
    }
    try:
        # Pull 7 days around the announcement to survive weekends/holidays.
        start = (announcement_date - timedelta(days=3)).date()
        end = (announcement_date + timedelta(days=6)).date()
        hist = yf.Ticker(ticker).history(start=start, end=end)
        if hist is None or hist.empty:
            return out
        # Normalise to naive midnight dates.
        import pandas as pd
        hist = hist.copy()
        hist.index = hist.index.tz_convert(None).normalize()
        ann_day = pd.Timestamp(announcement_date.replace(tzinfo=None)).normalize()
        sessions = list(hist.index)
        # Announcement session: last session <= ann_day (covers after-hours filings).
        prior_sessions = [s for s in sessions if s <= ann_day]
        if not prior_sessions:
            return out
        ann_session = prior_sessions[-1]
        after_sessions = [s for s in sessions if s > ann_session]
        if not after_sessions:
            out["context"] = "next session not yet available"
            return out
        next_session = after_sessions[0]
        ann_close = float(hist.loc[ann_session, "Close"])
        next_close = float(hist.loc[next_session, "Close"])
        out["next_session_pct"] = round((next_close - ann_close) / ann_close * 100, 3)
        out["context"] = f"close {ann_session.date()} -> close {next_session.date()}"
    except Exception as exc:
        print(f"[earnings] price fetch failed for {ticker}: {exc}", file=sys.stderr)
    return out


def load_prior_guidance(supabase_url: str, key: str, ticker: str) -> dict | None:
    """Return the most recent guidance row for this ticker, or None."""
    try:
        r = httpx.get(
            f"{supabase_url}/rest/v1/company_guidance",
            params={
                "ticker": f"eq.{ticker}",
                "select": "fiscal_period,guidance,filed_at",
                "order": "filed_at.desc",
                "limit": 1,
            },
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception as exc:
        print(f"[earnings] prior guidance fetch failed for {ticker}: {exc}", file=sys.stderr)
        return None


def build_bundle(
    *,
    name: str,
    ticker: str,
    sector: str,
    filing: dict,
    cik_no_zero: str,
    supabase_url: str | None,
    supabase_key: str | None,
) -> dict | None:
    result = fetch_ex_991(cik_no_zero, filing["accessionNumber"])
    if result is None:
        print(f"[earnings] {ticker}: EX-99.1 not found in {filing['accessionNumber']}", file=sys.stderr)
        return None
    ex_filename, press_release_text = result

    announcement = datetime.fromisoformat(
        filing["acceptanceDateTime"].replace("Z", "+00:00")
    )
    consensus = get_consensus(ticker)
    price_reaction = get_price_reaction(ticker, announcement)
    prior_guidance = None
    if supabase_url and supabase_key:
        prior_guidance = load_prior_guidance(supabase_url, supabase_key, ticker)

    acc_no_dash = filing["accessionNumber"].replace("-", "")
    filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zero}/{acc_no_dash}/{ex_filename}"

    return {
        "ticker": ticker,
        "company_name": name,
        "sector": sector,
        "fiscal_period": None,  # Claude extracts from the press release
        "filed_at": filing["acceptanceDateTime"],
        "press_release": {
            "text": press_release_text,
            "source_url": filing_url,
            "accession_number": filing["accessionNumber"],
        },
        "prepared_remarks": "not_available",
        "consensus": consensus,
        "prior_guidance": prior_guidance,
        "price_reaction": price_reaction,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble earnings input bundles for the writer")
    parser.add_argument("--ticker", help="Process only this ticker (for debugging)")
    parser.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    args = parser.parse_args()

    load_dotenv(override=True)
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/") or None
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.lookback_hours)

    try:
        cik_map = load_cik_map()
    except Exception as exc:
        print(f"[earnings] FATAL: SEC ticker map fetch failed: {exc}", file=sys.stderr)
        return 1

    targets = flat_watchlist()
    if args.ticker:
        targets = [t for t in targets if t[1].upper() == args.ticker.upper()]
        if not targets:
            print(f"[earnings] ticker {args.ticker} not in watchlist", file=sys.stderr)
            return 1

    bundles: list[dict] = []
    for name, ticker, sector in targets:
        cik = cik_map.get(ticker.upper())
        if not cik:
            continue
        try:
            filings = recent_earnings_filings(cik, cutoff)
        except Exception as exc:
            print(f"[earnings] {ticker}: submissions fetch failed: {exc}", file=sys.stderr)
            continue
        if not filings:
            continue
        # Most recent first.
        filings.sort(key=lambda f: f["acceptanceDateTime"], reverse=True)
        filing = filings[0]
        cik_no_zero = cik.lstrip("0")
        bundle = build_bundle(
            name=name, ticker=ticker, sector=sector, filing=filing,
            cik_no_zero=cik_no_zero,
            supabase_url=supabase_url, supabase_key=supabase_key,
        )
        if bundle:
            bundles.append(bundle)
            print(f"[earnings] {ticker}: bundled ({len(bundle['press_release']['text'])} PR chars)", file=sys.stderr)

    print(f"[earnings] {len(bundles)} bundle(s) assembled (lookback {args.lookback_hours}h)", file=sys.stderr)
    json.dump({"bundles": bundles, "generated_at": datetime.now(timezone.utc).isoformat()}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
