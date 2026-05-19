"""Generate structured earnings cards from input bundles produced by
earnings_backend.py.

Input (stdin or --input): JSON with {"bundles": [...]} where each bundle has
the ticker, press release text, consensus, prior guidance, and price reaction.

Output (stdout): JSON with {"earnings_cards": [...], "meta": {...}}.

Each card conforms to the Ascension Partners earnings card schema (see
EARNINGS_CARD_SYSTEM below).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv

SONNET_MODEL = "claude-sonnet-4-6"
SONNET_INPUT_PRICE_PER_MTOK = 3.0
SONNET_OUTPUT_PRICE_PER_MTOK = 15.0


EARNINGS_CARD_SYSTEM = """You are an equity analyst writing earnings summaries for Ascension Partners' Daily Digest. Your output feeds a structured dashboard where each company's earnings appears as its own card, read by fund managers on the buy-side.

Your job: produce a concise, investable summary of one company's quarterly earnings report, as structured JSON. The user message contains a single `<inputs>` block with all the source material. Produce exactly one card for that company.

<task>
Produce a single JSON object containing structured facts and a Digest paragraph. Every number in the paragraph must also appear in the structured fields. Write the structured fields first (mentally), then the paragraph, so the paragraph is grounded in the facts you committed to.
</task>

<output_schema>
{
  "ticker": string,
  "company_name": string,
  "fiscal_period": string,                // compact form only, e.g. "Q1 2026" or "FY 2025". No parenthetical dates.
  "headline": string,                    // 6-10 words for card title, e.g. "Microsoft beats on Azure, guidance raised"
  "tag": "beat" | "miss" | "in-line" | "mixed",
  "one_line_takeaway": string,           // <=20 words, the single most investable point
  "results": {
    "revenue_actual": number | null,     // FULL USD, e.g. $6.48B => 6480000000. Never store as millions or billions.
    "revenue_consensus": number | null,  // FULL USD. If the provided consensus is in different units than the press release reports, normalize to match revenue_actual.
    "revenue_surprise_pct": number | null,
    "eps_actual": number | null,         // USD per share (unscaled, e.g. 5.94)
    "eps_consensus": number | null,      // USD per share (unscaled)
    "eps_surprise_pct": number | null,
    "segment_highlights": [
      { "segment": string, "actual": number | null, "vs_consensus_pct": number | null, "note": string }
    ],
    "beat_miss_rationale": string        // one sentence, especially if tag is "mixed"
  },
  "guidance": {
    "direction": "raised" | "lowered" | "maintained" | "initiated" | "withdrawn" | "not_provided",
    "summary": string,                   // 1-2 sentences on outlook
    "key_changes": [string]              // specific revisions e.g. "FY revenue raised to $X-Y from $A-B"
  },
  "price_reaction": {
    "move_pct": number,                  // use next-session if available, else after-hours
    "move_context": "after-hours" | "next-session",
    "interpretation": string             // one sentence: why the market reacted this way
  },
  "digest_paragraph": string,            // 80-140 words, see requirements below
  "flags": {
    "low_confidence_fields": [string],
    "missing_data": [string]
  }
}
</output_schema>

<paragraph_requirements>
The digest_paragraph field:
- Length: 80-140 words.
- Stands alone. Do not assume the reader has seen the Market Wrap or any other context.
- Structure: (1) result vs consensus, (2) guidance and what changed, (3) price reaction and market interpretation.
- Lead with the most investable takeaway. If guidance matters more than the print itself, lead with guidance.
- Name specific numbers. Not "beat on revenue" but "beat revenue by 3% ($X vs $Y expected)".
- Include only the one or two segments that drove the result, not every reported segment.
- If the market reaction is counterintuitive (e.g. beat but stock fell), explain why explicitly.
- Tone: neutral, analytical, direct. No hype. Avoid filler like "it's worth noting" or "notably". Avoid adjectives like "strong" or "impressive" unless quoting management.
- Do not use em-dashes. Use commas, full stops, or parentheses.
- Vary sentence openings. Don't start every sentence with the company name.
- Written for sophisticated buy-side readers. Don't define common industry terms.
</paragraph_requirements>

<headline_requirements>
The headline field:
- 6-10 words.
- Captures the two most important facts (usually: beat/miss + guidance direction, OR the single biggest segment story).
- No clickbait, no questions, no ellipses.
- Examples of good headlines:
  - "Microsoft beats on Azure, full-year guidance raised"
  - "Amazon tops estimates but AWS deceleration concerns market"
  - "Boeing misses on 737 deliveries, withdraws FY guidance"
</headline_requirements>

<one_line_takeaway_requirements>
The one_line_takeaway field:
- <=20 words.
- The single sentence a fund manager would tell a colleague about this print.
- Should answer: "so what?" not "what happened?"
- Examples:
  - "AI capex cycle confirmed; Azure reacceleration validates Microsoft's infrastructure spend."
  - "Revenue beat masks margin compression in core retail; AWS did the heavy lifting."
</one_line_takeaway_requirements>

<critical_rules>
1. NEVER fabricate a number. If a number isn't in the source material, set it to null and omit from prose.
2. Every number in digest_paragraph, headline, and one_line_takeaway must appear in the source material or the provided consensus/price inputs.
3. If press release contradicts prepared remarks, flag it in low_confidence_fields and prefer the press release.
4. Guidance direction: compare new guidance to prior_guidance input. If prior_guidance is empty, use "not_provided" rather than guessing.
5. Tag classification — strictly mechanical from revenue_surprise_pct and eps_surprise_pct ONLY. Ignore YoY growth, "record" language, or adjusted vs GAAP framing when picking the tag (those belong in the paragraph, not the tag).
   - "beat" = both revenue_surprise_pct > 1 AND eps_surprise_pct > 1
   - "miss" = both revenue_surprise_pct < -1 AND eps_surprise_pct < -1
   - "in-line" = both within +/-1% of consensus
   - "mixed" = any other combination (e.g. rev beat but EPS miss). Explain in beat_miss_rationale.
6. If uncertain about any field, add it to flags.low_confidence_fields. Do not paper over uncertainty.
7. Call the `record_earnings_card` tool with the structured card. Do not return prose.
</critical_rules>
"""


_NULLABLE_NUMBER = {"type": ["number", "null"]}

TOOL_EARNINGS_CARD = {
    "name": "record_earnings_card",
    "description": "Record the structured earnings card for one company's quarterly print.",
    "input_schema": {
        "type": "object",
        "required": [
            "ticker", "company_name", "fiscal_period", "headline", "tag",
            "one_line_takeaway", "results", "guidance", "price_reaction",
            "digest_paragraph", "flags",
        ],
        "properties": {
            "ticker":            {"type": "string"},
            "company_name":      {"type": "string"},
            "fiscal_period":     {"type": "string"},
            "headline":          {"type": "string"},
            "tag":               {"type": "string", "enum": ["beat", "miss", "in-line", "mixed"]},
            "one_line_takeaway": {"type": "string"},
            "digest_paragraph":  {"type": "string"},
            "results": {
                "type": "object",
                "required": [
                    "revenue_actual", "revenue_consensus", "revenue_surprise_pct",
                    "eps_actual", "eps_consensus", "eps_surprise_pct",
                    "segment_highlights", "beat_miss_rationale",
                ],
                "properties": {
                    "revenue_actual":       _NULLABLE_NUMBER,
                    "revenue_consensus":    _NULLABLE_NUMBER,
                    "revenue_surprise_pct": _NULLABLE_NUMBER,
                    "eps_actual":           _NULLABLE_NUMBER,
                    "eps_consensus":        _NULLABLE_NUMBER,
                    "eps_surprise_pct":     _NULLABLE_NUMBER,
                    "segment_highlights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["segment", "actual", "vs_consensus_pct", "note"],
                            "properties": {
                                "segment":          {"type": "string"},
                                "actual":           _NULLABLE_NUMBER,
                                "vs_consensus_pct": _NULLABLE_NUMBER,
                                "note":             {"type": "string"},
                            },
                        },
                    },
                    "beat_miss_rationale": {"type": "string"},
                },
            },
            "guidance": {
                "type": "object",
                "required": ["direction", "summary", "key_changes"],
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["raised", "lowered", "maintained", "initiated", "withdrawn", "not_provided"],
                    },
                    "summary":     {"type": "string"},
                    "key_changes": {"type": "array", "items": {"type": "string"}},
                },
            },
            "price_reaction": {
                "type": "object",
                "required": ["move_pct", "move_context", "interpretation"],
                "properties": {
                    "move_pct":       {"type": "number"},
                    "move_context":   {"type": "string", "enum": ["after-hours", "next-session"]},
                    "interpretation": {"type": "string"},
                },
            },
            "flags": {
                "type": "object",
                "required": ["low_confidence_fields", "missing_data"],
                "properties": {
                    "low_confidence_fields": {"type": "array", "items": {"type": "string"}},
                    "missing_data":          {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


def _fmt_number(v) -> str:
    if v is None:
        return "not available"
    if isinstance(v, (int, float)):
        return f"{v}"
    return str(v)


def render_input_block(bundle: dict) -> str:
    cons = bundle.get("consensus") or {}
    price = bundle.get("price_reaction") or {}
    prior = bundle.get("prior_guidance")
    if prior and prior.get("guidance"):
        prior_text = (
            f"Prior guidance from {prior.get('fiscal_period')} "
            f"(filed {prior.get('filed_at')}):\n"
            f"{json.dumps(prior['guidance'], indent=2)}"
        )
    else:
        prior_text = "No prior guidance on file."

    # Consensus detail (all periods) so Claude can pick the right one if 0q rolled forward.
    rev_detail = cons.get("revenue_detail") or []
    eps_detail = cons.get("eps_detail") or []
    detail_lines = []
    if rev_detail:
        detail_lines.append("Revenue estimates by period:")
        for row in rev_detail:
            detail_lines.append(f"  {row['period']}: {row['avg']}")
    if eps_detail:
        detail_lines.append("EPS estimates by period:")
        for row in eps_detail:
            detail_lines.append(f"  {row['period']}: {row['avg']}")
    detail_block = "\n".join(detail_lines) if detail_lines else "(no analyst estimate detail)"

    return (
        "<inputs>\n"
        "<company>\n"
        f"Ticker: {bundle['ticker']}\n"
        f"Name: {bundle['company_name']}\n"
        f"Sector: {bundle['sector']}\n"
        f"Fiscal period: extract from press release\n"
        "</company>\n\n"
        "<consensus>\n"
        f"Revenue consensus (0q, current quarter analyst avg): {_fmt_number(cons.get('revenue_consensus'))}\n"
        f"EPS consensus (0q, current quarter analyst avg): {_fmt_number(cons.get('eps_consensus'))}\n"
        f"Key segment consensus: not available\n"
        f"Analyst estimate detail (for context if 0q has already rolled forward):\n{detail_block}\n"
        "</consensus>\n\n"
        "<prior_guidance>\n"
        f"{prior_text}\n"
        "</prior_guidance>\n\n"
        "<price_reaction>\n"
        f"After-hours move: {_fmt_number(price.get('after_hours_pct'))}\n"
        f"Next-session move: {_fmt_number(price.get('next_session_pct'))}\n"
        f"Context: {price.get('context') or 'n/a'}\n"
        "</price_reaction>\n\n"
        "<press_release>\n"
        f"{bundle['press_release']['text']}\n"
        "</press_release>\n\n"
        "<prepared_remarks>\n"
        f"{bundle.get('prepared_remarks') or 'not_available'}\n"
        "</prepared_remarks>\n"
        "</inputs>"
    )


REQUIRED_TOP_KEYS = {
    "ticker", "company_name", "fiscal_period", "headline", "tag",
    "one_line_takeaway", "results", "guidance", "price_reaction",
    "digest_paragraph", "flags",
}


def validate_card(card: dict, bundle: dict, warnings: list) -> dict:
    """Minimal structural validation. Log warnings for missing keys; do not mutate."""
    missing = REQUIRED_TOP_KEYS - card.keys()
    if missing:
        warnings.append(f"{bundle['ticker']}: card missing keys: {sorted(missing)}")
    # Always stamp canonical ticker/company_name so the UI layer can trust them.
    card["ticker"] = bundle["ticker"]
    card["company_name"] = bundle["company_name"]
    return card


def generate_card(client: anthropic.Anthropic, bundle: dict, warnings: list) -> tuple[dict | None, dict]:
    """Return (card_dict_or_None, usage_dict).

    Uses the Anthropic API's tool-use mechanism with forced tool_choice, so
    the model's output is schema-validated at the decoder level. The earlier
    retry+dump scaffolding for malformed JSON is no longer needed.
    """
    user_msg = render_input_block(bundle)

    try:
        resp = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=2500,
            system=[{
                "type": "text",
                "text": EARNINGS_CARD_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[TOOL_EARNINGS_CARD],
            tool_choice={"type": "tool", "name": TOOL_EARNINGS_CARD["name"]},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        warnings.append(f"{bundle['ticker']}: Claude call failed: {exc}")
        return None, {"input_tokens": 0, "output_tokens": 0}

    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}

    tool_input = None
    for block in (resp.content or []):
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_EARNINGS_CARD["name"]:
            tool_input = block.input
            break

    if not isinstance(tool_input, dict):
        warnings.append(f"{bundle['ticker']}: model did not invoke the earnings card tool")
        return None, usage

    card = validate_card(tool_input, bundle, warnings)
    card["_source"] = {
        "press_release_url": bundle["press_release"]["source_url"],
        "accession_number": bundle["press_release"]["accession_number"],
        "filed_at": bundle["filed_at"],
    }
    return card, usage


def load_input(path: str | None) -> dict:
    if path:
        with open(path) as f:
            return json.load(f)
    if not sys.stdin.isatty():
        return json.load(sys.stdin)
    raise SystemExit("earnings_writer: no input on stdin and no --input given")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate structured earnings cards from bundles")
    parser.add_argument("--input", help="Path to bundles JSON (defaults to stdin)")
    args = parser.parse_args()

    load_dotenv(override=True)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("earnings_writer: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    client = anthropic.Anthropic(api_key=api_key)

    payload = load_input(args.input)
    bundles = payload.get("bundles") or []
    warnings: list[str] = []
    cards: list[dict] = []
    total_in = 0
    total_out = 0
    api_calls = 0

    for bundle in bundles:
        api_calls += 1
        card, usage = generate_card(client, bundle, warnings)
        if usage:
            total_in += usage["input_tokens"]
            total_out += usage["output_tokens"]
        if card is not None:
            cards.append(card)

    cost = (
        total_in / 1_000_000 * SONNET_INPUT_PRICE_PER_MTOK
        + total_out / 1_000_000 * SONNET_OUTPUT_PRICE_PER_MTOK
    )

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "earnings_cards": cards,
        "meta": {
            "api_calls": api_calls,
            "bundles_in": len(bundles),
            "cards_out": len(cards),
            "estimated_cost_usd": round(cost, 4),
            "warnings": warnings,
        },
    }
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    print(
        f"[earnings_writer] {len(cards)} card(s), "
        f"{total_in} in / {total_out} out tokens, ${cost:.4f} est.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
