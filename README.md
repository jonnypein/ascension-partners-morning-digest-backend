# Morning Digest — Backend + Writer + Scheduler

Three-stage pipeline that generates and persists a publication-ready daily market digest:

1. **Backend** ([digest_backend.py](digest_backend.py)) — pulls structured market data (yfinance + FRED) and context snippets (RSS feeds classified by Claude Haiku).
2. **Writer** ([digest_writer.py](digest_writer.py)) — takes the backend output and generates editorial content (Market Wrap + per-company sections) via Claude Sonnet.
3. **Scheduler** ([run_daily.py](run_daily.py)) — fires the pipeline at 05:30 Europe/London on weekdays (skipping NYSE holidays) and saves output to `output/`.

## End-to-end usage

```bash
python digest_backend.py | python digest_writer.py > todays_digest.json
```

Or save intermediate output:

```bash
python digest_backend.py > backend.json
python digest_writer.py --input backend.json > todays_digest.json
```

Or run the writer alone — it will invoke the backend internally:

```bash
python digest_writer.py > todays_digest.json
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get API keys

**FRED** (free, instant): https://fred.stlouisfed.org/docs/api/api_key.html
Click "Request API Key", log in or create a free account, fill in a brief description.

**Anthropic**: https://console.anthropic.com — required for the context pipeline only.

### 3. Configure environment

```bash
cp .env.example .env
# Paste your keys into .env
```

### 4. Run

```bash
# Both pipelines (default)
python digest_backend.py

# Market data only — no Anthropic key required
python digest_backend.py --data-only

# Context only — no yfinance/FRED required
python digest_backend.py --context-only
```

JSON is printed to stdout. Two summary lines go to stderr.

To capture JSON only:

```bash
python digest_backend.py > output.json
```

## Output shape

```json
{
  "run_timestamp": "2026-04-17T07:00:00Z",
  "market_data": {
    "data_as_of": "2026-04-16",
    "equities": {
      "indices": [...],
      "us_sectors": [...],
      "watchlist": {
        "technology_software": [...],
        "industrials_energy_transport": [...],
        "financials": [...],
        "healthcare": [...],
        "real_estate": [...]
      }
    },
    "fixed_income": [...],
    "commodities": [...],
    "fx": [...],
    "macro": [...]
  },
  "context": {
    "window_hours": 24,
    "by_asset_class": {
      "equities": [...],
      "fixed_income": [...],
      "commodities": [...],
      "fx": [...],
      "macro": [...]
    },
    "stats": {
      "feeds_attempted": 8,
      "feeds_ok": 7,
      "items_fetched": 94,
      "items_classified": 94,
      "items_selected": 22,
      "api_calls": 8,
      "estimated_cost_usd": 0.0031
    }
  },
  "errors": [...]
}
```

### Market data record shapes

**Equities / sectors / watchlist / commodities / FX:**
`name`, `ticker`, `last`, `change_1d_pct`, `change_1w_pct`, `change_ytd_pct`

**Fixed income:**
`name`, `ticker`, `last_yield_pct`, `change_1d_bps`, `change_1w_bps`

**Macro (FRED):**
`series_id`, `name`, `latest_value`, `latest_date`, `prior_value`, `prior_date`

### Context item shape

`headline`, `description` (≤300 chars, HTML-stripped), `source`, `url`, `tags`, `relevance` (1–5), `published`

Tags: `equities`, `fixed_income`, `commodities`, `fx`, `macro`

## Earnings cards (separate per-company cards)

In parallel with the Market Wrap and company sections, each daily run also
produces structured earnings cards for any watchlist company that filed an
SEC 8-K with Item 2.02 ("Results of Operations") in the last 36 hours.

Pipeline:

1. **[earnings_backend.py](earnings_backend.py)** — walks `WATCHLIST`, finds
   CIKs via the public SEC ticker map, pulls recent 8-Ks from EDGAR, fetches
   each filing's EX-99.1 earnings release, adds consensus (rev + EPS via
   yfinance), next-session price reaction, and prior-quarter guidance from
   our own `company_guidance` table. Emits one bundle per company that reported.
2. **[earnings_writer.py](earnings_writer.py)** — sends each bundle to Sonnet
   using the Ascension Partners earnings card prompt. Returns a validated card
   per the schema (headline, tag, results, guidance, price_reaction,
   digest_paragraph, flags).
3. **[run_daily.py](run_daily.py)** upserts each card into the dedicated
   `earnings_cards` Supabase table (keyed on `(ticker, fiscal_period)` with
   a top-level `filed_at` for date-window queries), upserts each card's
   guidance into `company_guidance` for next quarter's `prior_guidance`
   lookup, and publishes the daily digest (Markets Wrap etc.) to `digests`
   as before.

Cards live in their own table — not inside the daily `digests` row — so they
surface on their actual filing date in Lovable rather than being pinned to
whichever pipeline run produced them. The frontend queries with a date
window (`filed_at >= now() - interval '24h'` for the "Earnings Today"
section).

Non-US filers (e.g. Shell files 6-K) produce zero hits and are skipped silently.

### One-time setup

Run [schema.sql](schema.sql) in the Supabase SQL editor to create the
`company_guidance` and `earnings_cards` tables.

### Manual usage

```bash
# Find any 8-Ks in last 36h for all watchlist companies
python earnings_backend.py

# Debug one ticker with a wider window
python earnings_backend.py --ticker JPM --lookback-hours 240

# Full pipeline (bundles -> cards)
python earnings_backend.py --lookback-hours 240 | python earnings_writer.py
```

## Writer output shape

The writer emits a single JSON object for downstream renderers (Lovable, email, PDF):

```json
{
  "generated_at": "2026-04-17T07:15:00Z",
  "data_as_of": "2026-04-16",
  "digest": {
    "market_wrap": { "title": "Markets Wrap – ...", "paragraphs": ["...", "...", "...", "..."] },
    "company_sections": [
      { "company_name": "...", "ticker": "...", "event_type": "earnings",
        "headline": "Q1 2026 Results", "paragraphs": ["...", "...", "..."] }
    ],
    "market_snapshot": { "indices": [...], "fixed_income": [...], "commodities": [...], "fx": [...] },
    "earnings_this_week": []
  },
  "meta": { "api_calls": 9, "estimated_cost_usd": 0.12, "warnings": [] }
}
```

- **Market Wrap** is written by Sonnet 4.6 across 4 asset-class paragraphs.
- **Company sections** are capped at 7 per day, each 3 paragraphs, written by Sonnet 4.6.
- **Company identification** uses Haiku 4.5 to scan context items for material events.
- `earnings_this_week` is reserved empty until a later module wires in a calendar.

## Notes on specific tickers

- **2YY=F** (US 2Y Treasury): CBOE yield futures — may have limited history, falls through to `errors` if unavailable.
- **^GDBR10** / **^GUKG10** (Bund / Gilt): not reliably available on Yahoo Finance — expected to appear in `errors`.
- **^STOXX** (STOXX Europe 600): verify this resolves correctly vs. EURO STOXX 50 on your data feed.

## Scheduler

[run_daily.py](run_daily.py) runs the backend → writer pipeline automatically.

```bash
# Run once immediately (bypasses schedule + holiday checks) — use to smoke test
python3 run_daily.py --now

# Start the long-running scheduler (fires 05:30 Europe/London Mon–Fri, skips NYSE holidays)
python3 run_daily.py
```

Output is written to:
- `output/digest_YYYY-MM-DD.json` — per-day file
- `output/digest_latest.json` — copy of the most recent successful run
- `output/backend_YYYY-MM-DD.json` — only on writer failure, for debugging

Logs go to `logs/run_daily.log` and stdout. Tail them with `tail -f logs/run_daily.log`.

The scheduler is DST-aware (uses `zoneinfo("Europe/London")`); no manual adjustment needed across BST/GMT transitions.

### Running in the background

Pick whichever fits your environment:

```bash
# nohup (simple, survives logout)
nohup python3 run_daily.py >/dev/null 2>&1 &

# tmux / screen (attach to check on it)
tmux new -s digest 'python3 run_daily.py'

# systemd user unit (Linux) — create ~/.config/systemd/user/digest.service then:
systemctl --user enable --now digest
```

## Requirements

- Python 3.11+
- Internet access to Yahoo Finance, api.stlouisfed.org, and api.anthropic.com
