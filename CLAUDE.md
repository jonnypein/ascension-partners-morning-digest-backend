# CLAUDE.md

Context for future Claude sessions on this repo. Keep it tight; update it
when pipelines, schemas, or conventions change.

## Start-of-session ritual

1. Read `PLAN.md` — the living roadmap. "Currently in progress" tells you
   where the last session stopped. "Next session" says what to do.
2. Whenever you call `TodoWrite` with meaningful status changes, mirror
   them into `PLAN.md`. The todo tool is in-session only; `PLAN.md` is
   the persistent copy so future sessions don't lose context.

## Project

Ascension Partners Daily Digest — a morning market-intelligence system
for a buy-side fund. Publishes to Supabase; rendered by a Lovable frontend
the user maintains separately.

## Pipelines

Five Python modules, each runnable standalone or chained. They all share
the SEC-fetching infrastructure in `company_profile_builder.py` and the
`.env`-gated Supabase writes in `publish.py`.

- **`run_daily.py`** — the orchestrator. `--now` runs the full pipeline
  once (bypasses weekend/holiday checks); `--scheduled` runs once with
  holiday check (used by GitHub Actions cron); no flag starts an
  in-process `schedule` loop that fires at 05:30 Europe/London.
- **`digest_backend.py`** — daily news ingestion. Pulls ~45 RSS feeds
  (CNBC, MarketWatch, Reuters via Google News, Bloomberg via Google News,
  Seeking Alpha, Federal Reserve, ECB, BoE, BoJ, Nikkei Asia, per-ticker
  Yahoo Finance) + yfinance prices + FRED macro series. Haiku
  classifier assigns asset-class tags and relevance 1-5. Outputs
  `{market_data, context}` JSON.
- **`digest_writer.py`** — Sonnet writers. Step A (Haiku) identifies
  watchlist company events; Step B (Sonnet) writes 3-paragraph company
  sections; Step C (Sonnet) writes the Market Wrap. Emits the full
  digest JSON read by Supabase.
- **`earnings_backend.py` + `earnings_writer.py`** — earnings card
  pipeline. Detects watchlist companies that filed 8-Ks with Item 2.02
  (Results of Operations) in the last 36h, fetches EX-99.1 via EDGAR,
  pulls consensus + price reaction from yfinance, sends to Sonnet with
  the Ascension earnings card prompt, upserts to `earnings_cards`.
  Guidance direction is separately upserted to `company_guidance` for
  next-quarter comparison.
- **`company_profile_builder.py`** — annual build. Fetches latest 10-K
  (fallback 20-F/40-F), extracts Item 1 "Business", Sonnet distils into
  structured profile. Upserts `company_profiles`. Shared SEC helpers
  (`load_cik_map`, `get_latest_annual_filing`, `fetch_primary_doc`,
  `flat_watchlist`) are imported by other builders.
- **`risk_profile_builder.py`** — annual build. Same flow as profiles
  but targets Item 1A "Risk Factors", returns 5-8 categorised risks with
  materiality ratings. Upserts `risk_profiles`.
- **`catalyst_builder.py`** — weekly refresh. yfinance `earnings_dates`
  → `catalysts` table. No Claude calls.
- **`publish.py`** — thin Supabase PostgREST wrapper. One function per
  table; all use `Prefer: resolution=merge-duplicates` so re-runs are
  idempotent. Uses `load_dotenv(override=True)` because the user's shell
  env has an empty `ANTHROPIC_API_KEY` that would otherwise shadow the
  `.env` file.

## Supabase

Project URL `https://lamzjkuyjvqkklnbdeng.supabase.co`. Schema in
`schema.sql` (run manually via Supabase SQL Editor; there's no
migration tooling).

- **`digests`** — one row per market session (`date` = data_as_of).
  Lovable reads latest.
- **`earnings_cards`** — one row per `(ticker, fiscal_period)`. Keyed
  by `filed_at` for date-windowed queries. Public read.
- **`company_guidance`** — one row per `(ticker, fiscal_period)`.
  Backend-only (no public read policy).
- **`company_profiles`** — one row per ticker. Public read.
- **`risk_profiles`** — one row per ticker. Public read.
- **`catalysts`** — many rows per ticker. Public read.

All tables have RLS enabled. The service role key (in `.env`) bypasses
RLS for writes; the anon key (Lovable) hits only the `public read`
policies.

## Environment

- Python **3.13** in `.venv/` at repo root. System `python3` is 3.9
  which won't work (lacks PEP 604 union syntax used in type hints).
- Activate with `.venv/bin/python` directly (no `source activate`
  needed).
- `.env` lives at repo root, not committed. Keys: `FRED_API_KEY`,
  `ANTHROPIC_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
- `load_dotenv(override=True)` everywhere — the shell has an empty
  `ANTHROPIC_API_KEY` set somewhere (maybe Claude Code) that otherwise
  masks the real key.

## GitHub Actions

`.github/workflows/run-daily.yml` runs `run_daily.py --scheduled` on
weekdays at 04:30 UTC. Runs in practice land at ~06:30 UTC because of
GH's well-documented scheduled-job queue delays. That's fine — US
markets don't open until 13:30 UTC.

## Commands that come up

```bash
# One-off full pipeline (bypasses holiday check)
.venv/bin/python run_daily.py --now

# Smoke-test a single earnings card end-to-end
.venv/bin/python earnings_backend.py --ticker JPM --lookback-hours 240 \
  | .venv/bin/python earnings_writer.py

# Refresh company profiles for one ticker (manual review first)
.venv/bin/python company_profile_builder.py --ticker MSFT --no-publish

# Refresh risk profiles across the watchlist
.venv/bin/python risk_profile_builder.py

# Refresh catalysts (fast, no Claude)
.venv/bin/python catalyst_builder.py
```

## Conventions

- Commit messages: short subject, 2-5-line body explaining *why*, no
  emojis, end with a `Co-Authored-By:` line for Claude.
- No emojis anywhere — in code, prompts, UI copy, or docs.
- Claude model strings: `claude-sonnet-4-6` for writers and
  `claude-haiku-4-5-20251001` for classifiers. Don't change without
  checking pricing constants.
- Prompts: favour structured XML tags (`<inputs>...`) over prose. Put
  large stable instructions in the `system` block with
  `cache_control: ephemeral` for cost savings.
- JSON extraction: `_parse_json` helpers tolerate code fences and
  surrounding prose. If a model still returns unparseable content,
  dump raw responses to `output/failed_parses_*.jsonl` for offline
  diagnosis (pattern used in `step_b_company_section`).

## Known gaps / parked work

- **Company profiles**: 4 tickers fail 10-K parsing (MS uses column-table
  TOC, SHEL files 20-F not 10-K, GE uses non-standard body headings).
  Live with the 30/34 hit rate.
- **Earnings cards**: the live-captured cards from this week missed GE,
  RTX, AXP, CBRE — probably filed outside the 36h lookback. Widen the
  window or investigate 8-K item codes before Monday's slot.
- **Transcripts**: parked. FMP Ultimate ($79/mo) or Motley Fool scrape
  are the two live options. Not yet wired.
- **House views overlay**: needs user-written theses per ticker. Table
  + admin UI not yet built.
- **Macro sensitivities, consensus framework**: planned for next week.
- **Phase 2d (memory, alerts, thesis evolution)**: parked indefinitely.

## Costs

- Full `run_daily.py`: ~$0.12 per run (~$2.50/month at weekday cadence).
- Earnings card: ~$0.04 per card. Peak earnings week maybe $0.30/day.
- Annual company-profile rebuild: ~$1.40 one-off across 34 tickers.
- Annual risk-profile rebuild: ~$1.50 one-off across 34 tickers.
- Catalysts refresh: free (yfinance only).
- Total annual: well under $50.
