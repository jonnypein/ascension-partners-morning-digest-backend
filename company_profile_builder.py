"""Build a structured company profile from the latest 10-K Item 1 (Business)
section, for each watchlist ticker. Feeds the `/companies/:ticker` Lovable
page (Phase 2 "research-note library").

Pipeline per ticker:
    1. Look up CIK from the SEC ticker map
    2. Find most recent 10-K (fallback: 20-F for foreign private issuers)
    3. Fetch the primary filing doc, extract Item 1 "Business" text
    4. Send Item 1 to Sonnet with a structured-extraction prompt
    5. Upsert the resulting profile to Supabase's company_profiles table

Usage:
    python company_profile_builder.py                   # all watchlist tickers
    python company_profile_builder.py --ticker MSFT     # single ticker
    python company_profile_builder.py --ticker MSFT --no-publish   # dry run, print only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import anthropic
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from digest_backend import WATCHLIST

UA = "Ascension Partners Morning Digest jonathan.o.k.pein@gmail.com"
SEC_REQUEST_DELAY = 0.12
SEC_TIMEOUT = 30
SEC_CLIENT = httpx.Client(headers={"User-Agent": UA}, timeout=SEC_TIMEOUT)

SONNET_MODEL = "claude-sonnet-4-6"
SONNET_INPUT_PRICE_PER_MTOK = 3.0
SONNET_OUTPUT_PRICE_PER_MTOK = 15.0
MAX_ITEM_1_CHARS = 120_000   # ~30k tokens; Item 1 sections are rarely larger


PROFILE_SYSTEM = """You are an equity research analyst. You are given Item 1 "Business" from a company's 10-K filing. Extract a structured company profile for a buy-side daily digest system.

Output a single JSON object with this schema:
{
  "business_description": string,      // 2-3 paragraphs (150-250 words), investor-grade prose summarising what the company does and how it makes money. No bullet points. No legal hedges.
  "revenue_segments": [                // Main reporting segments as disclosed in the 10-K
    {"name": string, "description": string, "pct_of_revenue_est": number | null}
  ],
  "geographic_exposure": [             // Regional revenue split if disclosed
    {"region": string, "pct": number | null}
  ],
  "key_products": [string],            // 5-10 named products or product lines (not entire categories)
  "primary_competitors": [string],     // 5-10 competitors NAMED explicitly in the text. Prefer ticker symbols if publicly traded.
  "hq_location": string,               // e.g. "Redmond, WA" or "Bentonville, AR, United States"
  "employee_count": number | null,     // Total employees or FTEs if disclosed
  "website": string                    // Primary corporate website URL
}

Rules:
- NEVER fabricate. If a number or fact isn't in the text, return null (or omit from lists).
- `pct_of_revenue_est`: only populate if the 10-K explicitly states the segment's percentage of revenue; otherwise null.
- `primary_competitors`: only companies the 10-K itself names as competitors. Do not infer.
- Return only the JSON object. No preamble, no markdown fences.
"""


def load_cik_map() -> dict[str, str]:
    r = SEC_CLIENT.get("https://www.sec.gov/files/company_tickers.json")
    r.raise_for_status()
    return {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in r.json().values()}


def get_latest_annual_filing(cik: str) -> dict | None:
    """Return the most recent 10-K, falling back to 20-F for foreign private issuers."""
    time.sleep(SEC_REQUEST_DELAY)
    r = SEC_CLIENT.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    r.raise_for_status()
    recent = r.json()["filings"]["recent"]
    for preferred_form in ("10-K", "20-F", "40-F"):
        for i, form in enumerate(recent["form"]):
            if form == preferred_form:
                return {
                    "form": form,
                    "accessionNumber": recent["accessionNumber"][i],
                    "primaryDocument": recent["primaryDocument"][i],
                    "filingDate": recent["filingDate"][i],
                }
    return None


def _clean_filing_text(raw_html: str) -> str:
    """Convert an SEC HTML document into flattened, heading-friendly text."""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    # SEC filings break capitalized headings across HTML cells, so a flattened
    # 10-K has "B USINESS", "R ISK FACTORS", "G ENERAL", etc. Rejoin single
    # leading letters into the following all-caps word when the second chunk
    # is >=4 letters. Exclude A and I as leading letters — they're legitimate
    # standalone English words and would otherwise fuse "PART I ITEM 1" into
    # "PART IITEM 1" and break heading detection.
    text = re.sub(r"\b([BCDEFGHJKLMNOPQRSTUVWXYZ]) ([A-Z]{4,})\b", r"\1\2", text)
    return text


def fetch_primary_doc(cik_no_zero: str, accession: str, primary_doc: str) -> tuple[str, str]:
    """Return (filing_url, flattened_text) for the 10-K narrative body.

    The `primaryDocument` in SEC's submissions.json is sometimes the XBRL
    instance rather than the narrative 10-K. When the named primary doc
    doesn't contain both "Item 1" and "Item 1A" markers, fall back to the
    filing index and try other HTM files in size order (largest first).
    """
    acc_no_dash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zero}/{acc_no_dash}"

    def _try(filename: str) -> tuple[str, str] | None:
        url = f"{base}/{filename}"
        time.sleep(SEC_REQUEST_DELAY)
        try:
            r = SEC_CLIENT.get(url)
            r.raise_for_status()
        except Exception:
            return None
        text = _clean_filing_text(r.text)
        if _ITEM_1_START_RX.search(text) and _ITEM_1A_START_RX.search(text):
            return url, text
        return None

    # First attempt: the primary document the submissions feed named.
    primary_result = _try(primary_doc)
    if primary_result:
        return primary_result

    # Fallback: walk the filing index, try other HTM files by size descending.
    time.sleep(SEC_REQUEST_DELAY)
    try:
        r = SEC_CLIENT.get(f"{base}/index.json")
        r.raise_for_status()
        items = r.json()["directory"]["item"]
    except Exception:
        return f"{base}/{primary_doc}", ""

    candidates = [
        it for it in items
        if it["name"].endswith(".htm")
        and it["name"] != primary_doc
        and not it["name"].startswith("R")          # XBRL-rendered exhibits
        and "FilingSummary" not in it["name"]
    ]
    try:
        candidates.sort(key=lambda it: int(it.get("size", 0)), reverse=True)
    except Exception:
        pass

    for it in candidates:
        found = _try(it["name"])
        if found:
            return found

    # Nothing worked — return the primary doc URL with whatever text we got.
    return f"{base}/{primary_doc}", ""


# 10-K headings are unreliably spaced after HTML-to-text flattening (e.g.
# "B USINESS", "RIS KFACTORS"), so we don't require the trailing "Business"
# or "Risk Factors" phrase. Instead, the largest-span pair between "Item 1"
# and "Item 1A" markers identifies the real section — TOC references have
# tiny spans; the body section has 40-100k chars between them. The `\b`
# after the digit excludes "Item 10", "Item 11", "Item 1A", "Item 1B", etc.
_ITEM_1_START_RX = re.compile(r"(?i)item\s*1\b")
_ITEM_1A_START_RX = re.compile(r"(?i)item\s*1a\b")


ITEM_1_MIN_SPAN = 3000  # chars. Smaller than this is TOC row or cross-reference.


def extract_item_1(text: str) -> str:
    """Extract Item 1 (Business) from the full 10-K text.

    10-Ks contain "Item 1" / "Item 1A" references in multiple places:
    the table of contents, forward-looking cross-references like
    'See Item 1A of Part I — "Risk Factors"', the real section headings,
    and internal back-references. To avoid grabbing a cross-reference as
    the section boundary:

    * Require a minimum span of ITEM_1_MIN_SPAN chars — real Item 1
      sections are always tens of thousands of characters.
    * Among qualifying (start, end) pairs, pick the smallest `end` so we
      stop at the first real Item 1A heading rather than overshooting
      into later references embedded in Item 1A's own prose.
    """
    starts = [m.start() for m in _ITEM_1_START_RX.finditer(text)]
    ends = [m.start() for m in _ITEM_1A_START_RX.finditer(text)]
    if not starts or not ends:
        return ""
    candidates = []
    for e in ends:
        preceding_starts = [s for s in starts if s < e]
        if not preceding_starts:
            continue
        s = max(preceding_starts)  # nearest start before this end
        if e - s >= ITEM_1_MIN_SPAN:
            candidates.append((e, s))
    if not candidates:
        return ""
    # Sort by `e` ascending — smallest end wins (first real Item 1A heading).
    candidates.sort()
    e, s = candidates[0]
    chunk = text[s:e].strip()
    if len(chunk) > MAX_ITEM_1_CHARS:
        chunk = chunk[:MAX_ITEM_1_CHARS]
    return chunk


def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _parse_json(text: str) -> Optional[Any]:
    s = _strip_fences(text)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None
    return None


def build_profile(
    client: anthropic.Anthropic,
    ticker: str,
    company_name: str,
    sector: str,
    item_1: str,
    filing: dict,
    cik_no_zero: str,
) -> tuple[dict | None, int, int]:
    """Call Claude to extract a structured profile. Returns (profile_dict | None, in_tokens, out_tokens)."""
    user_msg = (
        f"Company: {company_name} ({ticker})\n"
        f"Filing: {filing['form']} filed {filing['filingDate']}\n\n"
        f"<item_1_business>\n{item_1}\n</item_1_business>"
    )
    try:
        resp = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=2500,
            system=[{
                "type": "text",
                "text": PROFILE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        print(f"[profile] {ticker}: Claude call failed: {exc}", file=sys.stderr)
        return None, 0, 0

    in_t = resp.usage.input_tokens if resp.usage else 0
    out_t = resp.usage.output_tokens if resp.usage else 0
    text = resp.content[0].text if resp.content else ""
    parsed = _parse_json(text)
    if not isinstance(parsed, dict):
        print(f"[profile] {ticker}: unparseable model output", file=sys.stderr)
        return None, in_t, out_t

    acc_no_dash = filing["accessionNumber"].replace("-", "")
    filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zero}/{acc_no_dash}/{filing['primaryDocument']}"

    profile = {
        "ticker":                  ticker,
        "company_name":            company_name,
        "sector":                  sector,
        "business_description":    parsed.get("business_description"),
        "revenue_segments":        parsed.get("revenue_segments") or [],
        "geographic_exposure":     parsed.get("geographic_exposure") or [],
        "key_products":            parsed.get("key_products") or [],
        "primary_competitors":     parsed.get("primary_competitors") or [],
        "hq_location":             parsed.get("hq_location"),
        "employee_count":          parsed.get("employee_count"),
        "website":                 parsed.get("website"),
        "source_filing_url":       filing_url,
        "source_filing_accession": filing["accessionNumber"],
        "refreshed_at":            datetime.now(timezone.utc).isoformat(),
    }
    return profile, in_t, out_t


def flat_watchlist() -> list[tuple[str, str, str]]:
    out = []
    for sector, items in WATCHLIST.items():
        for name, ticker in items:
            out.append((name, ticker, sector))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Build company profiles from 10-K Item 1")
    parser.add_argument("--ticker", help="Process only this ticker")
    parser.add_argument("--no-publish", action="store_true", help="Skip Supabase upsert; print profile to stdout")
    args = parser.parse_args()

    load_dotenv(override=True)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("company_profile_builder: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    client = anthropic.Anthropic(api_key=api_key)

    try:
        cik_map = load_cik_map()
    except Exception as exc:
        print(f"FATAL: SEC ticker map fetch failed: {exc}", file=sys.stderr)
        return 1

    targets = flat_watchlist()
    if args.ticker:
        targets = [t for t in targets if t[1].upper() == args.ticker.upper()]
        if not targets:
            print(f"ticker {args.ticker} not in watchlist", file=sys.stderr)
            return 1

    from publish import publish_company_profile

    total_in = 0
    total_out = 0
    ok = 0
    fail = 0
    for name, ticker, sector in targets:
        cik = cik_map.get(ticker.upper())
        if not cik:
            print(f"[profile] {ticker}: no CIK in SEC map", file=sys.stderr)
            fail += 1
            continue
        try:
            filing = get_latest_annual_filing(cik)
            if not filing:
                print(f"[profile] {ticker}: no 10-K / 20-F / 40-F found", file=sys.stderr)
                fail += 1
                continue
            cik_no_zero = cik.lstrip("0")
            _, text = fetch_primary_doc(cik_no_zero, filing["accessionNumber"], filing["primaryDocument"])
            item_1 = extract_item_1(text)
            if not item_1 or len(item_1) < 500:
                print(f"[profile] {ticker}: Item 1 extraction too short ({len(item_1)} chars) — skipping", file=sys.stderr)
                fail += 1
                continue
        except Exception as exc:
            print(f"[profile] {ticker}: fetch/extract failed: {exc}", file=sys.stderr)
            fail += 1
            continue

        profile, in_t, out_t = build_profile(client, ticker, name, sector, item_1, filing, cik_no_zero)
        total_in += in_t
        total_out += out_t
        if not profile:
            fail += 1
            continue

        if args.no_publish:
            print(json.dumps(profile, indent=2))
        else:
            try:
                publish_company_profile(profile)
                print(f"[profile] {ticker}: upserted ({filing['form']} {filing['filingDate']}, {len(item_1)} chars Item 1)", file=sys.stderr)
                ok += 1
            except Exception as exc:
                print(f"[profile] {ticker}: publish failed: {exc}", file=sys.stderr)
                fail += 1

    cost = total_in / 1_000_000 * SONNET_INPUT_PRICE_PER_MTOK + total_out / 1_000_000 * SONNET_OUTPUT_PRICE_PER_MTOK
    print(
        f"[profile] done: {ok} ok / {fail} failed  |  {total_in:,} in / {total_out:,} out tokens  |  ${cost:.4f}",
        file=sys.stderr,
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
