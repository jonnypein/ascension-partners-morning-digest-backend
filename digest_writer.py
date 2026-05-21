#!/usr/bin/env python3
"""
digest_writer.py — Step 3: editorial writer over digest_backend.py output.

Reads the combined market_data + context JSON and produces a publication-ready
digest as structured JSON on stdout. Downstream renderers (Lovable, email, PDF)
consume this directly.

Pipelines:
  A. Haiku identifies up to 7 watchlist companies with material news.
  B. Sonnet writes a 3-paragraph section for each identified company.
  C. Sonnet writes the 4-paragraph Market Wrap over full market_data + context.
  D. Pure transform assembles the numeric market_snapshot block.
  E. All pieces merged into the final JSON object.

Usage:
  python digest_writer.py                                  # runs digest_backend.py internally
  python digest_writer.py --input backend_output.json      # read from file
  python digest_backend.py | python digest_writer.py       # read from stdin
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

# Fix SSL on macOS before any network libs load.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

import anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

# USD per token
HAIKU_INPUT_PRICE   = 0.80  / 1_000_000
HAIKU_OUTPUT_PRICE  = 4.00  / 1_000_000
SONNET_INPUT_PRICE  = 3.00  / 1_000_000
SONNET_OUTPUT_PRICE = 15.00 / 1_000_000

MAX_COMPANY_SECTIONS = 7

EVENT_TYPES = {"earnings", "corporate_action", "guidance", "analyst", "regulatory", "capital_markets", "other"}


# ══════════════════════════════════════════════════════════════════════════════
# PARSING / HTTP HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _parse_json(text: str) -> Optional[Any]:
    """Parse JSON from model output; tolerate code fences and surrounding prose."""
    stripped = _strip_fences(text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Try largest {...} or [...] span
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, stripped, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return None


def _extract_tool_input(resp, tool_name: str) -> Optional[dict]:
    """Pull the .input dict from a tool_use block matching `tool_name`.

    With `tool_choice` forcing a specific tool, the API guarantees the
    response contains exactly one tool_use block with a schema-valid
    `.input`. We still search defensively in case the SDK ever returns
    extra content blocks.
    """
    for block in (resp.content or []):
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return block.input  # already a dict, schema-enforced by the API
    return None


def _round2(v):
    return round(v, 2) if isinstance(v, (int, float)) else None

def _round1(v):
    return round(v, 1) if isinstance(v, (int, float)) else None

def _fmt_pct(v):
    return f"{v:+.2f}%" if isinstance(v, (int, float)) else "n/a"

def _fmt_bps(v):
    return f"{v:+.1f}bps" if isinstance(v, (int, float)) else "n/a"


# ══════════════════════════════════════════════════════════════════════════════
# INPUT LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_input(input_path: Optional[str]) -> dict:
    """Load backend JSON from --input path, piped stdin, or subprocess run."""
    if input_path:
        with open(input_path, "r") as f:
            return json.load(f)
    if not sys.stdin.isatty():
        return json.load(sys.stdin)

    # No input specified — invoke digest_backend.py next to this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    backend_path = os.path.join(script_dir, "digest_backend.py")
    print(f"No input supplied — running {backend_path}...", file=sys.stderr)
    proc = subprocess.run(
        [sys.executable, backend_path],
        capture_output=True,
        text=True,
        cwd=script_dir,
    )
    # Pass through backend's own stderr summary
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"digest_backend.py exited with code {proc.returncode}")
    return json.loads(proc.stdout)


# ══════════════════════════════════════════════════════════════════════════════
# COST TRACKING
# ══════════════════════════════════════════════════════════════════════════════

class CostTracker:
    def __init__(self):
        self.haiku_in = 0
        self.haiku_out = 0
        self.sonnet_in = 0
        self.sonnet_out = 0
        self.api_calls = 0

    def record(self, model: str, usage) -> None:
        self.api_calls += 1
        if not usage:
            return
        if model == HAIKU_MODEL:
            self.haiku_in  += usage.input_tokens
            self.haiku_out += usage.output_tokens
        elif model == SONNET_MODEL:
            self.sonnet_in  += usage.input_tokens
            self.sonnet_out += usage.output_tokens

    def estimated_cost(self) -> float:
        return round(
            self.haiku_in  * HAIKU_INPUT_PRICE
            + self.haiku_out * HAIKU_OUTPUT_PRICE
            + self.sonnet_in  * SONNET_INPUT_PRICE
            + self.sonnet_out * SONNET_OUTPUT_PRICE,
            6,
        )


# ══════════════════════════════════════════════════════════════════════════════
# STEP A — company identification (Haiku)
# ══════════════════════════════════════════════════════════════════════════════

COMPANY_ID_SYSTEM = """You are a senior editor at an institutional market intelligence desk. Your job: identify which watchlist companies have newsworthy coverage worth a dedicated section in today's digest for a buy-side reader.

INCLUDE any of the following when a watchlist company is the primary subject:
- earnings reports, pre-announcements, earnings previews ahead of known print dates
- M&A, spinoffs, stake changes, major capital return decisions
- company-issued guidance (raise, cut, reaffirmation, withdrawal)
- sell-side analyst moves: ratings changes, price-target revisions, initiations, category calls — tag these as "analyst"
- regulatory or legal events (investigations, settlements, approvals, fines)
- capital markets activity: debt issuance, equity raises, secondary offerings, IPOs, share buybacks announced or executed, tender offers, credit facility arrangements — tag as "capital_markets"
- private credit and fundraising: fund closes, LP commitments, strategy launches for alt managers (BX, KKR, APO, BLK); private credit deal announcements — tag as "capital_markets"
- AI and technology developments that meaningfully affect the named company: AI infrastructure deals, chip partnerships, data center expansions, model releases, AI M&A, AI-driven product pivots — tag as "corporate_action" or "other"
- material partnerships, customer wins, large contracts
- executive departures or hires at CEO/CFO/key division head level
- strategic pivots, layoffs, restructurings, cyber incidents, outages
- index inclusion/removal, credit rating changes
- significant share-price moves on news (>3%)

EXCLUDE:
- Pure sector or asset-class commentary that doesn't single out a watchlist name ("tech leads," "banks rally")
- Routine operational updates with no new information
- Marketing PR or minor product refreshes without financial implication

event_type values:
- "earnings"         — results, pre-announcements, previews tied to an upcoming print
- "corporate_action" — M&A, spinoffs, exec changes, restructurings, layoffs, strategic pivots, partnerships, AI/tech deals
- "guidance"         — the company itself updating forward-looking expectations
- "analyst"          — sell-side ratings, price targets, initiations
- "regulatory"       — lawsuits, regulator actions, compliance events
- "capital_markets"  — debt/equity issuance, IPOs, secondaries, tender offers, buybacks, credit facilities, private credit deals, fund closes, LP commitments
- "other"            — any material catalyst that doesn't cleanly fit above

For each qualifying company, supply one entry in the tool's `companies` array. Maximum 7 entries — pick the highest-signal ones if you have more than 7 candidates. Prioritize earnings/M&A/regulatory over analyst chatter when ranking. Pass an empty array only if no watchlist company has any qualifying coverage in the context.

`company_name` and `ticker` must match the provided watchlist exactly. `relevant_context_indices` lists 0-based indices into the provided context items.

Call the `record_company_identifications` tool with your selection."""


TOOL_IDENTIFY_COMPANIES = {
    "name": "record_company_identifications",
    "description": "Record the watchlist companies that have material news worth a dedicated section.",
    "input_schema": {
        "type": "object",
        "required": ["companies"],
        "properties": {
            "companies": {
                "type": "array",
                "maxItems": MAX_COMPANY_SECTIONS,
                "items": {
                    "type": "object",
                    "required": ["company_name", "ticker", "event_type", "headline", "relevant_context_indices"],
                    "properties": {
                        "company_name": {"type": "string"},
                        "ticker": {"type": "string"},
                        "event_type": {"type": "string", "enum": sorted(EVENT_TYPES)},
                        "headline": {"type": "string"},
                        "relevant_context_indices": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0},
                        },
                    },
                },
            },
        },
    },
}


def step_a_identify_companies(
    client: anthropic.Anthropic,
    input_data: dict,
    cost: CostTracker,
    warnings: list,
) -> list[dict]:
    md  = input_data.get("market_data") or {}
    ctx = input_data.get("context") or {}

    # Flatten watchlist to (name, ticker) pairs
    watchlist: list[tuple[str, str]] = []
    for group in (md.get("equities", {}) or {}).get("watchlist", {}).values():
        for item in group:
            watchlist.append((item["name"], item["ticker"]))

    # Pull equity + macro context items (equity primary, macro sometimes drives stocks)
    by_class = (ctx.get("by_asset_class") or {})
    context_items = list(by_class.get("equities", [])) + list(by_class.get("macro", []))

    if not watchlist or not context_items:
        return []

    wl_lines = "\n".join(f"  - {n} ({t})" for n, t in watchlist)
    ctx_lines = []
    for i, item in enumerate(context_items):
        desc = (item.get("description") or "")[:250]
        ctx_lines.append(
            f"[{i}] {item.get('headline', '')}\n"
            f"     source: {item.get('source', '')} | relevance: {item.get('relevance', '')}\n"
            f"     {desc}"
        )

    user_msg = (
        f"Watchlist companies:\n{wl_lines}\n\n"
        f"Context items (index shown in brackets):\n\n"
        + "\n\n".join(ctx_lines)
    )

    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1024,
            system=COMPANY_ID_SYSTEM,
            tools=[TOOL_IDENTIFY_COMPANIES],
            tool_choice={"type": "tool", "name": TOOL_IDENTIFY_COMPANIES["name"]},
            messages=[{"role": "user", "content": user_msg}],
        )
        cost.record(HAIKU_MODEL, resp.usage)
        tool_input = _extract_tool_input(resp, TOOL_IDENTIFY_COMPANIES["name"])
        if tool_input is None:
            warnings.append("Step A: model did not invoke the identification tool")
            return []
        companies = tool_input.get("companies") or []
    except Exception as exc:
        warnings.append(f"Step A failed: {exc}")
        return []

    valid_tickers = {t for _, t in watchlist}
    results: list[dict] = []
    for entry in companies[:MAX_COMPANY_SECTIONS]:
        if not isinstance(entry, dict):
            continue
        ticker = entry.get("ticker", "").strip()
        if ticker not in valid_tickers:
            warnings.append(f"Step A: dropped non-watchlist ticker '{ticker}'")
            continue
        event_type = entry.get("event_type", "other")
        if event_type not in EVENT_TYPES:
            event_type = "other"
        indices = entry.get("relevant_context_indices", []) or []
        snippets = [context_items[i] for i in indices if isinstance(i, int) and 0 <= i < len(context_items)]
        results.append({
            "company_name": entry.get("company_name", "").strip(),
            "ticker": ticker,
            "event_type": event_type,
            "headline": entry.get("headline", "News Update").strip() or "News Update",
            "relevant_context_items": snippets,
        })
    return results


# ══════════════════════════════════════════════════════════════════════════════
# STEP B — company sections (Sonnet)
# ══════════════════════════════════════════════════════════════════════════════

COMPANY_SECTION_SYSTEM = """You are writing a section of a daily market intelligence digest covering a specific company's news.

STRUCTURE — exactly 3 paragraphs:
1. The numbers / the event — what happened. For earnings: revenue, EPS, key segment performance vs consensus, YoY comparisons. For other events: the facts of what occurred.
2. Context and forward-looking — management commentary, guidance changes, strategic implications, what investors will watch next.
3. Market reaction and takeaway — share price move (premarket, intraday, or close as appropriate), analyst consensus read, concise investable takeaway.

VOICE: trader-formal, third person, dense, facts-first. No "we" or "I", no hype ("groundbreaking"), no filler openings ("In a significant development"). Use shorthand: y/y, q/q, bps, bn, pp. Include consensus comparisons with "vs" or "beat/missed." Attribute analyst views where relevant.

CRITICAL — no fabrication: every number and every fact must come from the provided context snippets. If you don't have a number for something (e.g. precise consensus estimate), use language like "broadly in line with consensus" rather than inventing a figure. If the context is thin, write a thinner section rather than padding.

Call the `record_company_section` tool with the 3 paragraphs in order."""


TOOL_COMPANY_SECTION = {
    "name": "record_company_section",
    "description": "Record the 3-paragraph company section.",
    "input_schema": {
        "type": "object",
        "required": ["paragraphs"],
        "properties": {
            "paragraphs": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {"type": "string", "minLength": 1},
            },
        },
    },
}


def step_b_company_section(
    client: anthropic.Anthropic,
    company: dict,
    cost: CostTracker,
    warnings: list,
) -> dict:
    snippets = company.get("relevant_context_items", [])
    snippet_text = "\n\n".join(
        f"[{s.get('source','')}] {s.get('headline','')}\n{s.get('description','')}"
        for s in snippets
    ) or "(no context snippets provided — write only what can be justified by the event_type label.)"

    user_msg = (
        f"Company: {company['company_name']} ({company['ticker']})\n"
        f"Event type: {company['event_type']}\n"
        f"Event headline: {company['headline']}\n\n"
        f"Context snippets:\n\n{snippet_text}"
    )

    sources = [
        {
            "url":       s.get("url"),
            "headline":  s.get("headline"),
            "publisher": s.get("source"),
        }
        for s in snippets
        if s.get("url")
    ]

    shell = {
        "company_name": company["company_name"],
        "ticker":       company["ticker"],
        "event_type":   company["event_type"],
        "headline":     company["headline"],
        "sources":      sources,
    }

    try:
        resp = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=1500,
            system=COMPANY_SECTION_SYSTEM,
            tools=[TOOL_COMPANY_SECTION],
            tool_choice={"type": "tool", "name": TOOL_COMPANY_SECTION["name"]},
            messages=[{"role": "user", "content": user_msg}],
        )
        cost.record(SONNET_MODEL, resp.usage)
    except Exception as exc:
        warnings.append(f"Step B: {company['ticker']} crashed: {exc}")
        return {**shell, "paragraphs": [f"(section generation failed: {exc})"]}

    tool_input = _extract_tool_input(resp, TOOL_COMPANY_SECTION["name"])
    if tool_input is None or not tool_input.get("paragraphs"):
        # Forced tool_choice means this shouldn't happen, but guard anyway.
        warnings.append(f"Step B: {company['ticker']} did not invoke the section tool")
        return {**shell, "paragraphs": ["(writer output unavailable)"]}

    return {**shell, "paragraphs": [str(p) for p in tool_input["paragraphs"]]}


# ══════════════════════════════════════════════════════════════════════════════
# STEP C — Market Wrap (Sonnet)
# ══════════════════════════════════════════════════════════════════════════════

MARKET_WRAP_SYSTEM = """You are the writer of a daily market intelligence digest for institutional investors and portfolio managers. Your Market Wrap must explain what drove the prior session across asset classes — not simply list moves.

STRUCTURE — 4 paragraphs, 400–600 words total:
1. Equities — indices, sector leadership/laggards, notable single-stock drivers. Connect moves to drivers.
2. Fixed Income — Treasury yields (10Y anchor, 2Y/30Y when relevant), credit spreads, rate repricing, central bank commentary.
3. Commodities & FX — oil (Brent primary), gold, industrial metals where relevant. FX: DXY, EUR/USD standing; JPY, CHF, ZAR when material.
4. Macro & Look-ahead — released economic data (CPI, payrolls, PMI, retail sales, Fed commentary), plus the key catalyst on today's calendar.

VOICE:
- Trader-formal. Third person. No "we" or "I." No hype ("groundbreaking"), no filler openings ("In a significant development").
- Facts lead, explanation follows.
- Use shorthand: ~ for approximations, y/y, q/q, bps, bn, pp.
- Percentages with appropriate precision: "+0.80%" for indices, "~4%" for rough moves.
- Em-dashes for inline context and apposition.
- Attribution phrases for editorial judgement: "Investors read the move as...", "Analysts flagged...", "Markets are pricing..."

CRITICAL — no fabrication: every number must come from the provided market data block; every explanation must come from the provided context snippets. If a paragraph has data but no context to explain the move, state the move without speculating on drivers — do NOT invent reasoning. If an asset class has no material moves to report, keep that paragraph brief or fold it into an adjacent paragraph.

CHART HINTS — alongside the prose, tag the tickers whose charts most match what each paragraph discusses. The frontend uses these to render small sparklines next to the relevant paragraph. Rules:
- Use ticker strings EXACTLY as shown in MARKET DATA above (e.g. ^GSPC, XLE, NVDA, ^TNX, BZ=F, DX-Y.NYB). Do not invent tickers.
- paragraph_index is 0-based: paragraph 1 (equities) is 0, paragraph 4 (macro) is 3.
- timeframe matches the horizon you anchor on in that paragraph: "1d" for today's move, "1w" for the week's, "ytd" for cumulative context.
- importance: 1 = the chart most central to the day's narrative, 2 = secondary, 3 = supporting context.
- No more than 6 hints across the whole wrap. Per paragraph, 0-2 hints is typical. Omit entirely if no specific ticker materially adds to the prose (commonly the macro/look-ahead paragraph).

Call the `record_market_wrap` tool with a short thematic title, the 4 paragraphs in order (equities, fixed income, commodities & FX, macro & look-ahead), and chart hints."""


TOOL_MARKET_WRAP = {
    "name": "record_market_wrap",
    "description": "Record the daily 4-paragraph Market Wrap with a short thematic title, plus chart hints for each paragraph.",
    "input_schema": {
        "type": "object",
        "required": ["title", "paragraphs"],
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "paragraphs": {
                "type": "array",
                "minItems": 4,
                "maxItems": 4,
                "items": {"type": "string", "minLength": 1},
            },
            "chart_hints": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "required": ["ticker", "paragraph_index", "timeframe", "importance"],
                    "properties": {
                        "ticker":          {"type": "string", "minLength": 1},
                        "paragraph_index": {"type": "integer", "minimum": 0, "maximum": 3},
                        "timeframe":       {"type": "string", "enum": ["1d", "1w", "ytd"]},
                        "importance":      {"type": "integer", "minimum": 1, "maximum": 3},
                    },
                },
            },
        },
    },
}


def _valid_chart_tickers(md: dict) -> set[str]:
    """Collect every ticker symbol present in this market_data dict.

    Used to validate chart_hints emitted by the writer — any hint whose
    ticker isn't in this set gets dropped, since the frontend has no
    time-series for it. Macro/FRED series_ids are excluded on purpose;
    they don't render as the same kind of sparkline.
    """
    tickers: set[str] = set()
    eq = md.get("equities", {}) or {}
    for i in eq.get("indices", []) or []:
        if i.get("ticker"):
            tickers.add(i["ticker"])
    for s in eq.get("us_sectors", []) or []:
        if s.get("ticker"):
            tickers.add(s["ticker"])
    for group_items in (eq.get("watchlist") or {}).values():
        for it in group_items:
            if it.get("ticker"):
                tickers.add(it["ticker"])
    for fi in md.get("fixed_income", []) or []:
        if fi.get("ticker"):
            tickers.add(fi["ticker"])
    for c in md.get("commodities", []) or []:
        if c.get("ticker"):
            tickers.add(c["ticker"])
    for fx in md.get("fx", []) or []:
        if fx.get("ticker"):
            tickers.add(fx["ticker"])
    return tickers


def _format_market_data_for_wrap(md: dict) -> str:
    lines: list[str] = []
    eq = md.get("equities", {}) or {}

    lines.append("INDICES:")
    for i in eq.get("indices", []) or []:
        lines.append(
            f"  {i['name']} ({i['ticker']}): last {i.get('last')}, "
            f"1d {_fmt_pct(i.get('change_1d_pct'))}, "
            f"1w {_fmt_pct(i.get('change_1w_pct'))}, "
            f"YTD {_fmt_pct(i.get('change_ytd_pct'))}"
        )

    sectors = [s for s in (eq.get("us_sectors") or []) if isinstance(s.get("change_1d_pct"), (int, float))]
    sectors.sort(key=lambda s: s["change_1d_pct"], reverse=True)
    if sectors:
        lines.append("\nUS SECTORS (sorted by 1d move):")
        for s in sectors:
            lines.append(
                f"  {s['name']} ({s['ticker']}): "
                f"1d {_fmt_pct(s.get('change_1d_pct'))}, "
                f"1w {_fmt_pct(s.get('change_1w_pct'))}, "
                f"YTD {_fmt_pct(s.get('change_ytd_pct'))}"
            )

    lines.append("\nWATCHLIST (single stocks by group):")
    for group, items in (eq.get("watchlist") or {}).items():
        lines.append(f"  [{group}]")
        for it in items:
            lines.append(
                f"    {it['name']} ({it['ticker']}): last {it.get('last')}, "
                f"1d {_fmt_pct(it.get('change_1d_pct'))}, "
                f"YTD {_fmt_pct(it.get('change_ytd_pct'))}"
            )

    lines.append("\nFIXED INCOME:")
    for fi in md.get("fixed_income", []) or []:
        lines.append(
            f"  {fi['name']} ({fi['ticker']}): yield {fi.get('last_yield_pct')}%, "
            f"1d {_fmt_bps(fi.get('change_1d_bps'))}, "
            f"1w {_fmt_bps(fi.get('change_1w_bps'))}"
        )

    lines.append("\nCOMMODITIES:")
    for c in md.get("commodities", []) or []:
        lines.append(
            f"  {c['name']} ({c['ticker']}): last {c.get('last')}, "
            f"1d {_fmt_pct(c.get('change_1d_pct'))}, "
            f"1w {_fmt_pct(c.get('change_1w_pct'))}, "
            f"YTD {_fmt_pct(c.get('change_ytd_pct'))}"
        )

    lines.append("\nFX:")
    for fx in md.get("fx", []) or []:
        lines.append(
            f"  {fx['name']} ({fx['ticker']}): last {fx.get('last')}, "
            f"1d {_fmt_pct(fx.get('change_1d_pct'))}, "
            f"1w {_fmt_pct(fx.get('change_1w_pct'))}"
        )

    lines.append("\nMACRO (latest FRED observations):")
    for m in md.get("macro", []) or []:
        delta = ""
        lv, pv = m.get("latest_value"), m.get("prior_value")
        if isinstance(lv, (int, float)) and isinstance(pv, (int, float)):
            delta = f" (Δ {lv - pv:+.2f} vs prior {m.get('prior_date')})"
        lines.append(
            f"  {m['name']} [{m['series_id']}]: {lv} as of {m.get('latest_date')}{delta}"
        )

    return "\n".join(lines)


def _format_context_for_wrap(by_class: dict) -> str:
    parts: list[str] = []
    for cls in ("equities", "fixed_income", "commodities", "fx", "macro"):
        items = by_class.get(cls) or []
        if not items:
            continue
        parts.append(f"\n[{cls.upper()}]")
        for it in items:
            parts.append(
                f"- ({it.get('source', '')}) {it.get('headline', '')}\n"
                f"  {(it.get('description') or '')[:300]}"
            )
    return "\n".join(parts) if parts else "(no context items available)"


def step_c_market_wrap(
    client: anthropic.Anthropic,
    input_data: dict,
    cost: CostTracker,
    warnings: list,
) -> dict:
    md  = input_data.get("market_data") or {}
    ctx = input_data.get("context") or {}
    by_class = (ctx.get("by_asset_class") or {})

    user_msg = (
        f"MARKET DATA (prior session, data_as_of = {md.get('data_as_of', 'unknown')}):\n\n"
        f"{_format_market_data_for_wrap(md)}\n\n"
        f"CONTEXT (explanatory drivers from news, grouped by asset class):\n"
        f"{_format_context_for_wrap(by_class)}"
    )

    empty = {"title": "Markets Wrap", "paragraphs": []}

    try:
        resp = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=2500,
            system=MARKET_WRAP_SYSTEM,
            tools=[TOOL_MARKET_WRAP],
            tool_choice={"type": "tool", "name": TOOL_MARKET_WRAP["name"]},
            messages=[{"role": "user", "content": user_msg}],
        )
        cost.record(SONNET_MODEL, resp.usage)
    except Exception as exc:
        warnings.append(f"Step C (Market Wrap) crashed: {exc}")
        return empty

    tool_input = _extract_tool_input(resp, TOOL_MARKET_WRAP["name"])
    if tool_input is None or not tool_input.get("paragraphs"):
        warnings.append("Step C: Market Wrap did not invoke the wrap tool")
        return empty

    valid_tickers = _valid_chart_tickers(md)
    filtered_hints: list[dict] = []
    for h in (tool_input.get("chart_hints") or []):
        if not isinstance(h, dict):
            continue
        ticker = h.get("ticker")
        if ticker not in valid_tickers:
            warnings.append(f"Step C: dropped chart_hint with unknown ticker '{ticker}'")
            continue
        filtered_hints.append({
            "ticker":          ticker,
            "paragraph_index": h.get("paragraph_index"),
            "timeframe":       h.get("timeframe"),
            "importance":      h.get("importance"),
        })

    return {
        "title":       str(tool_input.get("title") or "Markets Wrap"),
        "paragraphs":  [str(p) for p in tool_input["paragraphs"]],
        "chart_hints": filtered_hints,
    }


# ══════════════════════════════════════════════════════════════════════════════
# STEP D — numeric market snapshot (pure transform)
# ══════════════════════════════════════════════════════════════════════════════

def step_d_market_snapshot(input_data: dict) -> dict:
    md = input_data.get("market_data") or {}
    eq = md.get("equities", {}) or {}
    return {
        "indices": [
            {
                "name":           i.get("name"),
                "ticker":         i.get("ticker"),
                "last":           _round2(i.get("last")),
                "change_1d_pct":  _round2(i.get("change_1d_pct")),
                "change_1w_pct":  _round2(i.get("change_1w_pct")),
                "change_ytd_pct": _round2(i.get("change_ytd_pct")),
            }
            for i in (eq.get("indices") or [])
        ],
        "fixed_income": [
            {
                "name":            fi.get("name"),
                "ticker":          fi.get("ticker"),
                "last_yield_pct":  _round2(fi.get("last_yield_pct")),
                "change_1d_bps":   _round1(fi.get("change_1d_bps")),
                "change_1w_bps":   _round1(fi.get("change_1w_bps")),
            }
            for fi in (md.get("fixed_income") or [])
        ],
        "commodities": [
            {
                "name":           c.get("name"),
                "ticker":         c.get("ticker"),
                "last":           _round2(c.get("last")),
                "change_1d_pct":  _round2(c.get("change_1d_pct")),
                "change_1w_pct":  _round2(c.get("change_1w_pct")),
                "change_ytd_pct": _round2(c.get("change_ytd_pct")),
            }
            for c in (md.get("commodities") or [])
        ],
        "fx": [
            {
                "name":           fx.get("name"),
                "ticker":         fx.get("ticker"),
                "last":           _round2(fx.get("last")),
                "change_1d_pct":  _round2(fx.get("change_1d_pct")),
                "change_1w_pct":  _round2(fx.get("change_1w_pct")),
            }
            for fx in (md.get("fx") or [])
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 3 — editorial writer over digest_backend.py output."
    )
    parser.add_argument(
        "--input",
        help="Path to backend JSON output. Otherwise reads stdin, or runs digest_backend.py.",
    )
    args = parser.parse_args()

    input_data = load_input(args.input)

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set — digest writer cannot run.", file=sys.stderr)
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    warnings: list[str] = []
    cost = CostTracker()

    generated_at = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    data_as_of   = (input_data.get("market_data") or {}).get("data_as_of", "")

    # A — company identification
    companies = step_a_identify_companies(client, input_data, cost, warnings)

    # B — company sections
    company_sections = [
        step_b_company_section(client, c, cost, warnings) for c in companies
    ]

    # C — market wrap
    market_wrap = step_c_market_wrap(client, input_data, cost, warnings)

    # D — numeric snapshot
    market_snapshot = step_d_market_snapshot(input_data)

    output = {
        "generated_at": generated_at,
        "data_as_of":   data_as_of,
        "digest": {
            "market_wrap":       market_wrap,
            "company_sections":  company_sections,
            "market_snapshot":   market_snapshot,
            "earnings_this_week": [],
        },
        "meta": {
            "api_calls":          cost.api_calls,
            "estimated_cost_usd": cost.estimated_cost(),
            "warnings":           warnings,
        },
    }

    print(json.dumps(output, indent=2))

    wrap_words = sum(len(p.split()) for p in market_wrap.get("paragraphs", []))
    print(
        f"Generated digest: {len(company_sections)} company sections, "
        f"market wrap {wrap_words} words, "
        f"${cost.estimated_cost():.2f} estimated cost.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
