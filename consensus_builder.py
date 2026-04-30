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
import os
import sys
from datetime import datetime, timezone

import httpx
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from company_profile_builder import flat_watchlist

EARNINGS_BEAT_HISTORY_QUARTERS = 8


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


def _growth_rates(t: yf.Ticker) -> dict:
    """Latest-quarter YoY growth and 5-year CAGR for revenue and EPS.

    yfinance returns only ~5 quarters and ~4 annual periods per ticker
    (free-tier limit). That's enough for a single-quarter YoY but not for
    a true TTM-vs-prior-TTM comparison; and 5-year CAGR is null for many
    tickers because we only have 4 annual datapoints. Forward growth
    rates (next quarter, next year) are separately captured in
    revenue_estimates / eps_estimates.
    """
    out = {
        "revenue_yoy_pct": None,
        "revenue_5y_cagr_pct": None,
        "eps_yoy_pct": None,
        "eps_5y_cagr_pct": None,
    }
    # Latest-quarter YoY: most recent quarter vs same quarter prior year
    try:
        q = t.quarterly_income_stmt
        if q is not None and not q.empty and len(q.columns) >= 5:
            for line, key in (("Total Revenue", "revenue_yoy_pct"), ("Diluted EPS", "eps_yoy_pct")):
                if line not in q.index:
                    continue
                row = q.loc[line]
                latest = row.iloc[0]
                year_ago = row.iloc[4]
                if pd.notna(latest) and pd.notna(year_ago) and float(year_ago) != 0:
                    out[key] = round((float(latest) - float(year_ago)) / abs(float(year_ago)) * 100, 2)
    except Exception as exc:
        print(f"[consensus] {t.ticker}: quarterly growth fetch failed: {exc}", file=sys.stderr)
    # 5-year CAGR — requires 5 annual datapoints; yfinance often returns 4.
    try:
        a = t.income_stmt
        if a is not None and not a.empty:
            for line, key in (("Total Revenue", "revenue_5y_cagr_pct"), ("Diluted EPS", "eps_5y_cagr_pct")):
                if line not in a.index:
                    continue
                row = a.loc[line].dropna()
                if len(row) < 5:
                    continue
                latest = float(row.iloc[0])
                base = float(row.iloc[4])
                if base > 0 and latest > 0:
                    out[key] = round(((latest / base) ** (1 / 5) - 1) * 100, 2)
    except Exception as exc:
        print(f"[consensus] {t.ticker}: annual growth fetch failed: {exc}", file=sys.stderr)
    return out


def _valuation_metrics(t: yf.Ticker) -> dict:
    """Forward P/E for current and next fiscal year, plus trailing Price/FCF
    and EV/EBITDA. Forward FCF and forward EBITDA require a paid data source
    so are not computed here — only forward P/E goes forward.
    """
    out = {
        "forward_pe_fy0": None,
        "forward_pe_fy1": None,
        "price_to_fcf_ttm": None,
        "ev_to_ebitda_ttm": None,
    }
    info = {}
    try:
        info = t.info or {}
    except Exception as exc:
        print(f"[consensus] {t.ticker}: .info fetch failed: {exc}", file=sys.stderr)
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    # Forward P/E for FY+0 and FY+1, both computed from earnings_estimate so
    # they're on the same fiscal-year basis. yfinance's `forwardPE` field is
    # NTM, which can differ from FY+0 due to fiscal-calendar offsets — we
    # avoid it here for consistency with FY+1.
    try:
        eps_est = t.earnings_estimate
        if isinstance(price, (int, float)) and eps_est is not None and not eps_est.empty:
            for period, key in (("0y", "forward_pe_fy0"), ("+1y", "forward_pe_fy1")):
                if period in eps_est.index:
                    eps_avg = eps_est.loc[period, "avg"]
                    if pd.notna(eps_avg) and float(eps_avg) > 0:
                        out[key] = round(float(price) / float(eps_avg), 2)
    except Exception as exc:
        print(f"[consensus] {t.ticker}: forward EPS estimate fetch failed: {exc}", file=sys.stderr)
    fcf, mcap = info.get("freeCashflow"), info.get("marketCap")
    if isinstance(fcf, (int, float)) and isinstance(mcap, (int, float)) and fcf > 0:
        out["price_to_fcf_ttm"] = round(float(mcap) / float(fcf), 2)
    ev, ebitda = info.get("enterpriseValue"), info.get("ebitda")
    if isinstance(ev, (int, float)) and isinstance(ebitda, (int, float)) and ebitda > 0:
        out["ev_to_ebitda_ttm"] = round(float(ev) / float(ebitda), 2)
    return out


def _eps_beat_history(t: yf.Ticker, n: int = EARNINGS_BEAT_HISTORY_QUARTERS) -> list[dict]:
    """Last n quarters of EPS actuals + estimates from yfinance. Revenue
    beats are not included — yfinance doesn't expose historical revenue
    consensus reliably; we'd need a paid feed to track those.
    """
    try:
        hist = t.earnings_history
    except Exception as exc:
        print(f"[consensus] {t.ticker}: earnings_history fetch failed: {exc}", file=sys.stderr)
        return []
    if hist is None or hist.empty:
        return []
    rows = []
    for idx, row in hist.head(n).iterrows():
        actual = row.get("epsActual")
        est = row.get("epsEstimate")
        surprise = row.get("surprisePercent")
        if pd.isna(actual) and pd.isna(est):
            continue
        beat = None
        if pd.notna(surprise):
            beat = float(surprise) > 0
        # yfinance reports surprisePercent as a fraction (0.07 = 7%); we use
        # percentage values throughout this codebase, so multiply by 100.
        rows.append({
            "period":       str(idx),
            "eps_actual":   None if pd.isna(actual) else float(actual),
            "eps_estimate": None if pd.isna(est) else float(est),
            "surprise_pct": None if pd.isna(surprise) else round(float(surprise) * 100, 2),
            "beat":         beat,
        })
    return rows


def _fiscal_year_end(t: yf.Ticker) -> str | None:
    try:
        info = t.info or {}
        ts = info.get("lastFiscalYearEnd")
        if ts:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except Exception:
        pass
    return None


def _non_gaap_eps_ttm_from_cards(ticker_sym: str, n: int = 4) -> float | None:
    """Sum eps_actual from the most recent n earnings cards in Supabase.
    Returns None when fewer than n cards exist (most tickers will populate
    naturally over the next 1-3 quarters as the earnings pipeline accumulates
    history). The cards capture whatever the press release reported, which
    for most issuers means the non-GAAP / "adjusted" figure.
    """
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        return None
    try:
        r = httpx.get(
            f"{url}/rest/v1/earnings_cards",
            params={
                "select": "card",
                "ticker": f"eq.{ticker_sym}",
                "order": "filed_at.desc",
                "limit": str(n),
            },
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception:
        return None
    rows = r.json()
    if len(rows) < n:
        return None
    eps_values = []
    for row in rows:
        results = (row.get("card") or {}).get("results") or {}
        eps = results.get("eps_actual")
        if isinstance(eps, (int, float)):
            eps_values.append(float(eps))
    if len(eps_values) < n:
        return None
    return round(sum(eps_values), 2)


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
    fundamentals = {
        "fiscal_year_end":   _fiscal_year_end(t),
        "growth":            _growth_rates(t),
        "valuation":         _valuation_metrics(t),
        "non_gaap_eps_ttm":  _non_gaap_eps_ttm_from_cards(ticker_sym),
        "eps_beat_history":  _eps_beat_history(t),
    }

    has_fundamentals = (
        fundamentals["fiscal_year_end"]
        or any(v is not None for v in fundamentals["growth"].values())
        or any(v is not None for v in fundamentals["valuation"].values())
        or fundamentals["non_gaap_eps_ttm"] is not None
        or fundamentals["eps_beat_history"]
    )
    if not (revenue or earnings or price_targets or recommendations or has_fundamentals):
        return None

    return {
        "ticker":            ticker_sym,
        "asof_date":         asof_date,
        "revenue_estimates": revenue or None,
        "eps_estimates":     earnings or None,
        "price_targets":     price_targets or None,
        "recommendations":   recommendations or None,
        "fundamentals":      fundamentals if has_fundamentals else None,
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
