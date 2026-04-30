# CLAUDE.md

Auto-loaded context for this repo. Human-facing setup/usage lives in [README.md](README.md). This is reference material — not a tutorial.

## Before doing anything

**`git pull` first.** This repo's main branch advances often. Don't trust the working tree; assume remote is ahead and sync before reading or editing. (A previous session burned 90 minutes by skipping this step.)

## What this repo produces

Four content streams, each writing to its own Supabase table that Lovable reads from:

| Table | Content | Pipeline | Cadence |
|---|---|---|---|
| `digests` | Market Wrap + per-company news sections + numeric snapshot | `digest_backend.py` → `digest_writer.py` → `publish_digest()` | Daily, weekday mornings (CI) |
| `earnings_cards` | Per-company structured earnings card from 8-K Item 2.02 filings | `earnings_backend.py` → `earnings_writer.py` → `publish_earnings_card()` | Daily; produces a card iff a watchlist company filed in last 36h |
| `company_guidance` | Forward-guidance block from each processed earnings card | Side effect of earnings pipeline → `publish_guidance()` | Same trigger as earnings_cards |
| `company_profiles` | 10-K Item 1 (Business) → structured profile per ticker | `company_profile_builder.py` → `publish_company_profile()` | **Manual only** — no schedule |

[run_daily.py](run_daily.py) orchestrates the first two (digest + earnings) in one process. The profile builder is standalone.

## Scheduling

- **GitHub Actions** at [.github/workflows/run-daily.yml](.github/workflows/run-daily.yml) runs `python run_daily.py --scheduled` at 04:30 UTC, Mon–Fri. This is the canonical scheduler. Run history: `gh run list --workflow=run-daily.yml`.
- `--scheduled` honours NYSE holiday/weekend skip; `--now` bypasses it.
- The legacy in-process scheduler in `run_daily.py:main()` (the `while True` loop using `schedule.every().monday.at(...)`) is dead code in production. Don't extend it; treat it as deprecated.
- Profiles are not on any schedule. Run `python company_profile_builder.py` (all tickers) or `--ticker XYZ` manually when you want to refresh.

## Load-bearing conventions

- **`WATCHLIST` group keys** in `digest_backend.py:80` (`technology_software`, `industrials_energy_transport`, `financials`, `healthcare`, `real_estate`) — the Lovable frontend renders these as section headers. Renaming/regrouping breaks the frontend.
- **`data_as_of`** is the date of the S&P 500's most recent close (`digest_backend.py`'s `run_market_data_pipeline`), not `datetime.now()`. Don't "fix" this — it's intentionally holiday/weekend-aware.
- **Digest output JSON shape** (top-level: `generated_at`, `data_as_of`, `digest.{market_wrap, company_sections, market_snapshot, earnings_this_week}`, `meta`) is exactly what `publish_digest()` writes into `digests.digest`. Add fields, don't rename.
- **Earnings cards: revenue/EPS in full USD** — `revenue_actual: 6480000000`, NOT `6.48` or `6480`. The schema in `earnings_writer.py`'s `EARNINGS_CARD_SYSTEM` is explicit. Lovable's number formatting depends on it.
- **`earnings_cards` keyed on `(ticker, fiscal_period)`** with `filed_at` lifted to a top-level column — this lets Lovable filter by date window without parsing jsonb. Re-runs upsert cleanly.
- **`company_profiles.refreshed_at`** has `default now()` — every upsert resets it. The Lovable cards' "Refreshed N days ago" stamp reads from this. Implication: that stamp tracks profile-builder runs, **not** daily digest freshness. Mixing the two is the trap that caused the original "9 days ago" confusion.
- **8-K filter**: only `Item 2.02` filings in last 36h count as earnings. Non-US filers (Shell files 6-K) produce 0 hits — skipped silently, not an error.
- **`earnings_this_week: []` in the digest** is a deliberate placeholder. Don't populate it from the earnings pipeline — those live in their own table. Keep emitted as `[]`.
- **Two tickers always fail and that's expected**: `^GDBR10` (Bund), `^GUKG10` (Gilt). They land in `errors`. Don't treat as a regression.

## Model assignments — do not swap

- **Haiku 4.5** (`claude-haiku-4-5-20251001`):
  - `digest_backend.py` — RSS classification (tags + relevance 1–5)
  - `digest_writer.py` Step A — pick up to 7 watchlist companies with material news
- **Sonnet 4.6** (`claude-sonnet-4-6`):
  - `digest_writer.py` Steps B/C — company sections + Market Wrap
  - `earnings_writer.py` — earnings cards
  - `company_profile_builder.py` — extract structured profile from 10-K Item 1
- Haiku for triage, Sonnet for prose. Cost numbers in each script's tracker assume this split.
- **All system prompts forbid fabrication.** Every number must come from the provided source material; no invented analyst commentary, no made-up consensus figures. Preserve this in any prompt edit.

## Config

- Env vars: `ANTHROPIC_API_KEY`, `FRED_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`. `.env.example` only lists the first two — Supabase keys live in GitHub Actions Secrets, plus your local `.env` if you run end-to-end on your machine.
- macOS system `python3` is often 3.9, too old (deps need 3.10+). CI uses 3.12. Locally, use a venv: `python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt` and run as `.venv/bin/python …`.
- Standalone-pipeline modes still work: `python digest_backend.py --data-only` (no Anthropic key required), `--context-only` (no yfinance/FRED required). `digest_writer.py`, `earnings_writer.py`, and `company_profile_builder.py` all require Anthropic. Anything that calls into `publish.py` requires Supabase env vars too.

## Dead / stale — do not edit or wire new code through these

- **`market_data.py`** — superseded by `digest_backend.py`'s market-data pipeline. Not imported anywhere. Delete on touch.
- **`todays_digest.json`** (repo root) — orphan from a one-off manual run. Not produced or consumed by any active code path.
- **`run_daily.py:main()` long-running loop** — see Scheduling above. CI uses `--scheduled`, not the in-process scheduler.

## Known issues

- **No alerting on workflow failure.** Check `gh run list --workflow=run-daily.yml` or the Actions tab; nobody pages.
- **GitHub Actions cron drift** 5–15 min during busy hours. Acceptable for a pre-market briefing.
- **Profile freshness ≠ digest freshness.** If the home-page cards look stale ("N days ago") but `gh run list` shows recent successful daily runs, the issue is `company_profiles.refreshed_at` — i.e. nobody has run `company_profile_builder.py` recently. Two different content systems, two different cadences.
- **No tests.** Smoke test locally with `.venv/bin/python run_daily.py --now`. Smoke test in CI by hitting "Run workflow" on the daily_digest action (workflow_dispatch trigger).
- **`output/` is gitignored** — daily JSON lives ephemerally in the workflow runner and is uploaded as an artifact (30-day retention). The canonical store is Supabase, not the repo.
