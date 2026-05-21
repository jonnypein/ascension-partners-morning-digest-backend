#!/usr/bin/env python3
"""
weekly_writer.py — Friday close-of-play market wrap.

Reads the same backend JSON shape as digest_writer.py (market_data + context)
and produces a weekly recap covering the past trading week. The orchestrator
(run_weekly.py) sets DIGEST_CONTEXT_WINDOW_HOURS=120 before running the backend
so the news context covers the full week, not just the last 24h.

Output shape (one JSON object on stdout):
  {
    "generated_at": "...Z",
    "week_ending":  "YYYY-MM-DD",
    "wrap": {
      "weekly_wrap":     {"title": "...", "paragraphs": [...]},
      "market_snapshot": {...}            # same shape as the daily digest
    },
    "meta": {"api_calls": int, "estimated_cost_usd": float, "warnings": [...]}
  }

Pipelines:
  C'. Sonnet writes the 5-paragraph Weekly Wrap over full market_data + context.
  D'. Pure transform assembles the numeric market_snapshot block.

Steps A and B (per-company sections) are intentionally skipped — those are
already produced by digest_writer.py Mon-Fri, and the weekly is meant to
sit alongside them as a market-level recap.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

import anthropic
from dotenv import load_dotenv

from digest_writer import (
    SONNET_MODEL,
    CostTracker,
    _extract_tool_input,
    _format_context_for_wrap,
    _format_market_data_for_wrap,
    _valid_chart_tickers,
    load_input,
    step_d_market_snapshot,
)

load_dotenv(override=True)


# ══════════════════════════════════════════════════════════════════════════════
# WEEKLY WRAP PROMPT
# ══════════════════════════════════════════════════════════════════════════════

WEEKLY_WRAP_SYSTEM = """You are the writer of a weekly market intelligence wrap for institutional investors and portfolio managers, published Friday after the US cash close. Your wrap explains what drove the week across asset classes — themes, not a chronology — and flags what next week is set up to deliver.

STRUCTURE — 5 paragraphs, 600–900 words total:
1. Equities — week's index moves (1w numbers anchor; reference YTD where relevant), sector leadership/laggards, the dominant theme of the week (e.g., earnings cycle, rate repricing, single-name dispersion). Name watchlist companies that drove tape with earnings or material news.
2. Fixed Income — 10Y anchor weekly move, 2Y/30Y when curve shape mattered, credit spreads, central-bank commentary or data prints that repriced the curve.
3. Commodities & FX — oil (Brent primary), gold, industrial metals where relevant. FX: DXY direction; the crosses that moved (EUR/USD, GBP/USD, AUD/USD, JPY, CHF, ZAR) tied to relative-rates or risk drivers.
4. Macro this week — the data prints and central-bank speakers that mattered: CPI, payrolls, PMI, retail sales, Fed/ECB/BoE/BoJ commentary. What they said about the trajectory.
5. Look-ahead — the key data, earnings, and central-bank events on next week's calendar. State what investors will be watching and why each one matters. If the catalyst calendar is light, say so.

VOICE:
- Trader-formal. Third person. Past tense for the week's review; present/future tense only in paragraph 5. No "we" or "I." No hype.
- Facts lead, explanation follows. Themes over chronology — do not write "On Monday... On Tuesday..." unless a single day's event was the dominant driver of the week.
- Use shorthand: ~ for approximations, w/w, y/y, q/q, bps, bn, pp.
- Index moves: percent with two decimals (+1.42%); rates: bps with one decimal (+8.4bps).
- Em-dashes for inline context.
- Attribution phrases for editorial judgement: "Investors read the move as...", "The week's dominant trade was...", "Markets are pricing..."

CRITICAL — no fabrication: every number must come from the provided market data block; every explanation must come from the provided context snippets. The market data shows 1d/1w/YTD moves — anchor on the 1w move. If a paragraph has data but no context to explain a move, state the move without speculating on drivers — do NOT invent reasoning. The paragraph 5 look-ahead should be drawn from forward-looking items in the context (earnings preview headlines, central bank meeting dates mentioned in source articles); if context has no clear forward catalysts, keep paragraph 5 brief and acknowledge the calendar quietly rather than inventing events.

CHART HINTS — alongside the prose, tag the tickers whose charts most match what each paragraph discusses. The frontend uses these to render sparklines next to the relevant paragraph. Rules:
- Use ticker strings EXACTLY as shown in MARKET DATA above (e.g. ^GSPC, XLE, NVDA, ^TNX, BZ=F, DX-Y.NYB). Do not invent tickers.
- paragraph_index is 0-based: paragraph 1 (equities) is 0, paragraph 5 (look-ahead) is 4.
- timeframe matches the horizon you anchor on in that paragraph: "1d" for a specific day's move, "1w" for the week's, "ytd" for cumulative context. Anchor weekly paragraphs on "1w" by default.
- importance: 1 = the chart most central to the week's narrative, 2 = secondary, 3 = supporting context.
- No more than 8 hints across the wrap. Per paragraph, 1-2 hints is typical. Omit for paragraphs where no specific ticker materially adds (the look-ahead paragraph often won't have any).

Call the `record_weekly_wrap` tool with a short thematic title, the 5 paragraphs in order, and chart hints."""


TOOL_WEEKLY_WRAP = {
    "name": "record_weekly_wrap",
    "description": "Record the Friday 5-paragraph weekly market wrap with a short thematic title, plus chart hints.",
    "input_schema": {
        "type": "object",
        "required": ["title", "paragraphs"],
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "paragraphs": {
                "type": "array",
                "minItems": 5,
                "maxItems": 5,
                "items": {"type": "string", "minLength": 1},
            },
            "chart_hints": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "required": ["ticker", "paragraph_index", "timeframe", "importance"],
                    "properties": {
                        "ticker":          {"type": "string", "minLength": 1},
                        "paragraph_index": {"type": "integer", "minimum": 0, "maximum": 4},
                        "timeframe":       {"type": "string", "enum": ["1d", "1w", "ytd"]},
                        "importance":      {"type": "integer", "minimum": 1, "maximum": 3},
                    },
                },
            },
        },
    },
}


def step_weekly_wrap(
    client: anthropic.Anthropic,
    input_data: dict,
    cost: CostTracker,
    warnings: list,
) -> dict:
    md  = input_data.get("market_data") or {}
    ctx = input_data.get("context") or {}
    by_class = (ctx.get("by_asset_class") or {})

    user_msg = (
        f"MARKET DATA (week ending {md.get('data_as_of', 'unknown')}):\n\n"
        f"{_format_market_data_for_wrap(md)}\n\n"
        f"CONTEXT (explanatory drivers and forward catalysts from this week's news, "
        f"grouped by asset class):\n"
        f"{_format_context_for_wrap(by_class)}"
    )

    empty = {"title": "Weekly Wrap", "paragraphs": []}
    try:
        resp = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=3500,
            system=WEEKLY_WRAP_SYSTEM,
            tools=[TOOL_WEEKLY_WRAP],
            tool_choice={"type": "tool", "name": TOOL_WEEKLY_WRAP["name"]},
            messages=[{"role": "user", "content": user_msg}],
        )
        cost.record(SONNET_MODEL, resp.usage)
    except Exception as exc:
        warnings.append(f"Weekly wrap step crashed: {exc}")
        return empty

    tool_input = _extract_tool_input(resp, TOOL_WEEKLY_WRAP["name"])
    if tool_input is None or not tool_input.get("paragraphs"):
        warnings.append("Weekly wrap: model did not invoke the wrap tool")
        return empty

    valid_tickers = _valid_chart_tickers(md)
    filtered_hints: list[dict] = []
    for h in (tool_input.get("chart_hints") or []):
        if not isinstance(h, dict):
            continue
        ticker = h.get("ticker")
        if ticker not in valid_tickers:
            warnings.append(f"Weekly wrap: dropped chart_hint with unknown ticker '{ticker}'")
            continue
        filtered_hints.append({
            "ticker":          ticker,
            "paragraph_index": h.get("paragraph_index"),
            "timeframe":       h.get("timeframe"),
            "importance":      h.get("importance"),
        })

    return {
        "title":       str(tool_input.get("title") or "Weekly Wrap"),
        "paragraphs":  [str(p) for p in tool_input["paragraphs"]],
        "chart_hints": filtered_hints,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def _last_friday(today: date) -> date:
    """Friday of this week if today is Fri-Sun; else the most recent past Friday."""
    # Mon=0..Sun=6. Friday=4.
    if today.weekday() <= 4:
        # Mon..Fri — the upcoming or current Friday is this week's Friday
        return today + timedelta(days=(4 - today.weekday()))
    # Sat or Sun — last Friday is 1 or 2 days back
    return today - timedelta(days=(today.weekday() - 4))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Friday weekly wrap writer over digest_backend.py output."
    )
    parser.add_argument(
        "--input",
        help="Path to backend JSON output. Otherwise reads stdin, or runs digest_backend.py.",
    )
    args = parser.parse_args()

    input_data = load_input(args.input)

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set — weekly writer cannot run.", file=sys.stderr)
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    warnings: list[str] = []
    cost = CostTracker()

    generated_at = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    md = input_data.get("market_data") or {}
    data_as_of_str = md.get("data_as_of") or ""
    try:
        data_as_of = date.fromisoformat(data_as_of_str)
    except ValueError:
        data_as_of = datetime.now(ZoneInfo("Europe/London")).date()
    week_ending = _last_friday(data_as_of)

    weekly_wrap = step_weekly_wrap(client, input_data, cost, warnings)
    market_snapshot = step_d_market_snapshot(input_data)

    output = {
        "generated_at": generated_at,
        "week_ending":  week_ending.isoformat(),
        "wrap": {
            "weekly_wrap":     weekly_wrap,
            "market_snapshot": market_snapshot,
        },
        "meta": {
            "api_calls":          cost.api_calls,
            "estimated_cost_usd": cost.estimated_cost(),
            "warnings":           warnings,
        },
    }

    print(json.dumps(output, indent=2))

    wrap_words = sum(len(p.split()) for p in weekly_wrap.get("paragraphs", []))
    print(
        f"Generated weekly wrap (week ending {week_ending}): "
        f"{wrap_words} words, ${cost.estimated_cost():.2f} estimated cost.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
