"""Distil Item 1A "Risk Factors" from each watchlist company's 10-K into a
structured risk profile. Feeds the Risk Profile section of the
`/companies/:ticker` Lovable page (Phase 2b).

Pipeline per ticker:
    1. Reuse company_profile_builder for CIK lookup, latest 10-K discovery,
       and flattened full-document text fetching.
    2. Slice out the Item 1A section (between "Item 1A" and "Item 1B/1C/2").
    3. Send Item 1A to Sonnet with a structured risk-extraction prompt.
    4. Upsert the resulting `{risks: [...]}` blob to Supabase.

Usage:
    python risk_profile_builder.py                 # all watchlist tickers
    python risk_profile_builder.py --ticker MSFT   # single ticker
    python risk_profile_builder.py --ticker MSFT --no-publish   # dry run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import anthropic
from dotenv import load_dotenv

from company_profile_builder import (
    HAIKU_INPUT_PRICE_PER_MTOK,
    HAIKU_OUTPUT_PRICE_PER_MTOK,
    SEC_REQUEST_DELAY,
    SONNET_INPUT_PRICE_PER_MTOK,
    SONNET_MODEL,
    SONNET_OUTPUT_PRICE_PER_MTOK,
    extract_section_via_llm,
    fetch_full_filing_text,
    fetch_primary_doc,
    flat_watchlist,
    get_latest_annual_filing,
    load_cik_map,
)

MAX_ITEM_1A_CHARS = 120_000  # ~30k tokens; Item 1A averages 20-40k words

# Distinguishing the real Item 1A body heading from TOC entries, in-text
# cross-references ("see Item 1A of this Form 10-K"), and the many "Item 1A"
# page-header repetitions inside the section itself is the hardest part of
# 10-K text extraction. 10-K formatting varies enormously between filers,
# so regex alone isn't enough — we use a content-shape test:
#
#   A real body heading is the FIRST "Item 1A" where:
#     * "Risk" appears within 30 chars after it (TOC + body both satisfy;
#       cross-refs like "Item 1A of this Form 10-K" don't)
#     * "Item 1B" does NOT appear within 100 chars after the "Risk" match
#       (TOC rows have "Item 1A ... Item 1B" in close succession; body has
#       thousands of chars of prose between Item 1A and Item 1B)
#
# Iterating left-to-right, the first match that passes both tests is the
# body start. Cross-references that pass the "Risk nearby" test appear
# later in the document and are skipped because we've already found the
# body. This approach is case-insensitive and works for filers that use
# mixed case ("Item 1A. Risk Factors") and filers that use all caps
# ("ITEM 1A. RISK FACTORS" with or without HTML-cell splitting like
# "RIS KFACTORS") uniformly.
_ITEM_1A_RX = re.compile(r"(?i)item\s*1a\b")
_RISK_NEAR_RX = re.compile(r"(?i)\brisk")
_ITEM_1B_NEAR_RX = re.compile(r"(?i)item\s*1b\b")

# End of Item 1A: next section in sequence. 10-Ks use 1B (Unresolved
# Staff Comments), 1C (Cybersecurity, since 2024), or 2 (Properties).
_ITEM_1A_END_RX = re.compile(r"(?i)item\s*(?:1b|1c|2)\b")


RISK_SYSTEM = """You are an equity research analyst. You are given Item 1A "Risk Factors" from a company's 10-K filing. Distil it into a structured risk profile for a buy-side daily digest.

Output a single JSON object with this schema:
{
  "risks": [
    {
      "category": "regulatory" | "competitive" | "operational" | "financial" | "geopolitical",
      "summary": string,       // 1-2 sentences, investor-grade, naming specifics where the 10-K does
      "materiality": "high" | "medium" | "low"
    }
  ]
}

Rules:
- Return 5-8 risks. Do not pad with generic boilerplate ("success depends on retaining key employees") unless it materially applies to this specific company (e.g. a pharmaceutical's key scientific talent).
- Prioritise the risks most likely to affect the stock price in the next 12 months.
- `materiality`: use "high" for risks that could move the stock >5% if realised, "medium" for meaningful-but-contained, "low" for structural background risk.
- `summary`: name the specific mechanism. Not "regulatory risk" but "EU Digital Markets Act could force Azure to unbundle from Microsoft 365, compressing margin."
- NEVER fabricate. If a named risk isn't in the provided Item 1A text, omit it.
- Return only the JSON object. No preamble, no markdown fences.
"""


def extract_item_1a(text: str) -> str:
    """Extract Item 1A from the flattened 10-K text.

    Uses the same largest-span heuristic as extract_item_1 in
    company_profile_builder: find all Item 1A starts and all Item 1B/1C/2
    ends, then pick the pair with the smallest end such that the span is
    at least ITEM_1A_MIN_SPAN — this avoids catching TOC entries or
    in-text cross-references like "as described above in Item 1A".
    """
    body_start = _find_body_start(text)
    if body_start is None:
        return ""
    ends = [m.start() for m in _ITEM_1A_END_RX.finditer(text)]
    following_ends = [e for e in ends if e > body_start + 3000]
    if not following_ends:
        return ""
    e = min(following_ends)
    chunk = text[body_start:e].strip()
    if len(chunk) > MAX_ITEM_1A_CHARS:
        chunk = chunk[:MAX_ITEM_1A_CHARS]
    return chunk


def _find_body_start(text: str) -> int | None:
    """Iterate Item 1A matches left-to-right; return the first that looks
    like a real section heading (Risk nearby, Item 1B not nearby)."""
    for m in _ITEM_1A_RX.finditer(text):
        window = text[m.end():m.end() + 200]
        risk_match = _RISK_NEAR_RX.search(window[:30])
        if not risk_match:
            continue
        # After the "Risk" anchor, the next 100 chars must NOT contain
        # "Item 1B" — that would indicate a TOC row.
        after_risk = window[risk_match.end():risk_match.end() + 100]
        if _ITEM_1B_NEAR_RX.search(after_risk):
            continue
        return m.start()
    return None


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


VALID_CATEGORIES = {"regulatory", "competitive", "operational", "financial", "geopolitical"}
VALID_MATERIALITY = {"high", "medium", "low"}


def build_risk_profile(
    client: anthropic.Anthropic,
    ticker: str,
    company_name: str,
    item_1a: str,
    filing: dict,
    cik_no_zero: str,
) -> tuple[dict | None, int, int]:
    user_msg = (
        f"Company: {company_name} ({ticker})\n"
        f"Filing: {filing['form']} filed {filing['filingDate']}\n\n"
        f"<item_1a_risk_factors>\n{item_1a}\n</item_1a_risk_factors>"
    )
    try:
        resp = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=2500,
            system=[{
                "type": "text",
                "text": RISK_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        print(f"[risk] {ticker}: Claude call failed: {exc}", file=sys.stderr)
        return None, 0, 0

    in_t = resp.usage.input_tokens if resp.usage else 0
    out_t = resp.usage.output_tokens if resp.usage else 0
    text = resp.content[0].text if resp.content else ""
    parsed = _parse_json(text)
    if not isinstance(parsed, dict):
        print(f"[risk] {ticker}: unparseable model output", file=sys.stderr)
        return None, in_t, out_t

    raw_risks = parsed.get("risks") or []
    if not isinstance(raw_risks, list):
        print(f"[risk] {ticker}: risks is not a list", file=sys.stderr)
        return None, in_t, out_t

    # Validate each entry: drop malformed rows, never mutate the model's content
    clean: list[dict] = []
    for r in raw_risks:
        if not isinstance(r, dict):
            continue
        cat = (r.get("category") or "").lower().strip()
        mat = (r.get("materiality") or "").lower().strip()
        summary = (r.get("summary") or "").strip()
        if cat not in VALID_CATEGORIES or mat not in VALID_MATERIALITY or not summary:
            continue
        clean.append({"category": cat, "summary": summary, "materiality": mat})

    if not clean:
        print(f"[risk] {ticker}: no valid risks parsed", file=sys.stderr)
        return None, in_t, out_t

    acc_no_dash = filing["accessionNumber"].replace("-", "")
    filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zero}/{acc_no_dash}/{filing['primaryDocument']}"

    return {
        "ticker":                  ticker,
        "risks":                   clean,
        "source_filing_url":       filing_url,
        "source_filing_accession": filing["accessionNumber"],
        "refreshed_at":            datetime.now(timezone.utc).isoformat(),
    }, in_t, out_t


def main() -> int:
    parser = argparse.ArgumentParser(description="Distil 10-K Item 1A into structured risk profiles")
    parser.add_argument("--ticker", help="Process only this ticker")
    parser.add_argument("--no-publish", action="store_true", help="Skip Supabase upsert; print profile")
    args = parser.parse_args()

    load_dotenv(override=True)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("risk_profile_builder: ANTHROPIC_API_KEY not set", file=sys.stderr)
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

    from publish import publish_risk_profile

    total_in = 0
    total_out = 0
    haiku_in = 0
    haiku_out = 0
    ok = 0
    fail = 0
    for name, ticker, sector in targets:
        cik = cik_map.get(ticker.upper())
        if not cik:
            print(f"[risk] {ticker}: no CIK in SEC map", file=sys.stderr)
            fail += 1
            continue
        try:
            filing = get_latest_annual_filing(cik)
            if not filing:
                print(f"[risk] {ticker}: no annual filing found", file=sys.stderr)
                fail += 1
                continue
            cik_no_zero = cik.lstrip("0")
            _, text = fetch_primary_doc(cik_no_zero, filing["accessionNumber"], filing["primaryDocument"])
            item_1a = extract_item_1a(text)
            if not item_1a or len(item_1a) < 500:
                # Regex path failed — fall back to LLM-based extraction with
                # broader input (primary + largest attachment) so wraparound
                # filings like WFC also have their EX-13 body available.
                print(
                    f"[risk] {ticker}: regex Item 1A too short ({len(item_1a)} chars) — trying LLM fallback",
                    file=sys.stderr,
                )
                full_text = fetch_full_filing_text(cik_no_zero, filing["accessionNumber"], filing["primaryDocument"])
                item_1a, usage = extract_section_via_llm(client, full_text, "Item 1A (Risk Factors)")
                haiku_in += usage.get("input_tokens", 0)
                haiku_out += usage.get("output_tokens", 0)
                if not item_1a or len(item_1a) < 500:
                    print(
                        f"[risk] {ticker}: LLM fallback also failed ({len(item_1a)} chars) — skipping",
                        file=sys.stderr,
                    )
                    fail += 1
                    continue
                print(f"[risk] {ticker}: LLM fallback succeeded ({len(item_1a)} chars)", file=sys.stderr)
        except Exception as exc:
            print(f"[risk] {ticker}: fetch/extract failed: {exc}", file=sys.stderr)
            fail += 1
            continue

        profile, in_t, out_t = build_risk_profile(client, ticker, name, item_1a, filing, cik_no_zero)
        total_in += in_t
        total_out += out_t
        if not profile:
            fail += 1
            continue

        if args.no_publish:
            print(json.dumps(profile, indent=2))
        else:
            try:
                publish_risk_profile(profile)
                print(f"[risk] {ticker}: upserted ({len(profile['risks'])} risks, {len(item_1a)} chars Item 1A)", file=sys.stderr)
                ok += 1
            except Exception as exc:
                print(f"[risk] {ticker}: publish failed: {exc}", file=sys.stderr)
                fail += 1

    sonnet_cost = total_in / 1_000_000 * SONNET_INPUT_PRICE_PER_MTOK + total_out / 1_000_000 * SONNET_OUTPUT_PRICE_PER_MTOK
    haiku_cost = haiku_in / 1_000_000 * HAIKU_INPUT_PRICE_PER_MTOK + haiku_out / 1_000_000 * HAIKU_OUTPUT_PRICE_PER_MTOK
    cost = sonnet_cost + haiku_cost
    print(
        f"[risk] done: {ok} ok / {fail} failed  |  Sonnet {total_in:,}/{total_out:,} + Haiku-fallback {haiku_in:,}/{haiku_out:,}  |  ${cost:.4f}",
        file=sys.stderr,
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
