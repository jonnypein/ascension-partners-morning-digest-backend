# PLAN.md

Living roadmap of work on this project. Read this at the start of every
session. Update it whenever you update `TodoWrite` with meaningful status
changes — otherwise the in-session todos vanish and future sessions lose
context.

Last updated: 2026-05-06 (Wednesday) — watchlist add + Friday weekly wrap

## Session 2026-05-06

- **Watchlist additions**: CAT (Caterpillar) and DE (Deere) added to
  `industrials_energy_transport`; RKT (Rocket Companies) added to
  `real_estate`. CBRE and Z (Zillow) were already on the list. All three
  new tickers validated against yfinance + SEC CIK map. They flow
  automatically through digest, earnings, profile/risk, catalyst,
  consensus, and sensitivity builders on next run.
- **AUD/USD added to FX** (`AUDUSD=X`) — alongside DXY, EUR/USD,
  GBP/USD, JPY, CHF, ZAR.
- **Friday close-of-play weekly wrap shipped** as a parallel pipeline:
  - `digest_backend.py` — `CONTEXT_WINDOW_HOURS` now reads
    `DIGEST_CONTEXT_WINDOW_HOURS` env (default 24). The weekly
    orchestrator sets it to 120 (5 trading days).
  - `weekly_writer.py` — Sonnet writes a 5-paragraph weekly recap
    (equities, FI, commodities/FX, macro this week, look-ahead).
    Reuses helpers from `digest_writer` (CostTracker, formatters,
    JSON parsing). No per-company sections — those stay daily.
  - `run_weekly.py` — orchestrator. `--now` runs unconditionally;
    `--scheduled` skips if today (London) is not Friday.
  - `weekly_wraps` table added to `schema.sql` (PK `week_ending`,
    public-read RLS) — **needs manual SQL migration** in Supabase
    SQL Editor before first cron fires.
  - `publish_weekly_wrap` added to `publish.py`.
  - GitHub Actions: `.github/workflows/weekly-wrap.yml` cron
    `0 22 * * 5` (Fri 22:00 UTC = 17:00 ET standard / 18:00 ET DST,
    always at least 1h post-NYSE-close; GH queue lands ~22:30-23:30
    UTC).
  - Smoke-tested locally 2026-05-06: backend with 120h lookback
    classified 1104 items, $0.16; writer produced clean 5-paragraph
    output, $0.13. ~$0.30/run, ~$15/year.

Last updated: 2026-04-30 (Thursday, late-morning) — out-of-band session

## Out-of-band session 2026-04-30

A separate Claude session (working directly in this worktree) did the
following while the active session was idle. Reconcile when picking up:

- **BLK duplicate row deleted** from `earnings_cards` (kept the clean
  `Q1 2026`, removed the verbose `Q1 2026 (three months ended...)` row).
- **AXP and CBRE Q1 2026 cards published retroactively.** Re-running
  the 240h-lookback bundles through `earnings_writer.py` produced
  parseable cards for both. The original Apr 24 cron run dropped them
  silently because the writer had no retry on transient JSON-parse
  failures.
- **`earnings_writer.py` hardened** with the same retry+dump pattern
  used in `digest_writer.py`. Two attempts; second adds stricter "JSON
  only" framing; both raw outputs dumped to
  `output/failed_parses_<date>.jsonl` if both fail. Adds
  `meta.bundles_in / meta.cards_out` so the artifact reveals drops
  without needing to grep workflow logs.
- **`run_daily.py`** now writes the writer's full payload (cards +
  meta) into `output/earnings_cards_<date>.json` and emits a WARN log
  when `cards_out < bundles_in`. Previously the artifact was just
  `{cards: [...]}`.
- **3 free builders re-run** for fresh data (catalysts, consensus
  snapshots, macro sensitivities). Tables now stamped 2026-04-30.
- **Yearly profile workflow added** (`.github/workflows/yearly_profiles.yml`)
  — fires April 1 each year, runs `company_profile_builder.py` and
  `risk_profile_builder.py` together. ~$5–7/yr. Combined fix for both
  10-K-derived datasets.
- **Diagnosis correction**: the original "earnings card capture gaps"
  hypothesis (lookback window too short) was wrong for all four
  tickers. AXP/CBRE filed within the 36h window — their issue was the
  writer's missing retry. **GE/RTX** filed within window too — their
  issue was that `earnings_backend.py:fetch_ex_991` only matched
  exhibit type `EX-99.1` exactly, but GE and RTX both file their
  press release as bare `EX-99` with no decimal. The 8-Ks were
  detected (Item 2.02 present) but no exhibit was found, so the
  bundle assembly silently returned None.
- **`earnings_backend.py` widened**: now matches EX-99.1, EX-99, or
  EX-99.<n>, preferring the most specific. GE and RTX **Q1 2026 cards
  published retroactively** after the fix.
- **Full week recap** in `earnings_cards` (after backfills): UNH, GE,
  RTX, CB (Apr 21); BA (Apr 22); BX, AXP, CBRE (Apr 23); V (Apr 28);
  GOOGL, MSFT, META, AMZN (Apr 29). 13/13 — zero gaps. AAPL pending
  on tomorrow's cron.
- **Profile / risk builders: 33/33 watchlist coverage now** (was
  28/33). Three-stage fix landed across two commits:
  1. SEC_TIMEOUT 30s -> 180s. Previously silently timed out on
     large 10-K HTMLs (MS/SHEL ~10MB each).
  2. LLM fallback (Haiku, ~$0.25/ticker, only on failures) when the
     regex-based section extractor returns empty. Handles
     unconventional headings.
  3. iXBRL header strip in `_clean_filing_text` — MS/SHEL embed
     thousands of us-gaap/fasb.org URIs in a hidden `<ix:header>`
     div that drowned narrative when BS4 flattened. Plus
     `fetch_full_filing_text` concatenates primary + largest
     attachment (largest first) so wraparound filings (WFC's body
     in EX-13) get full coverage.
  - All 5 originally broken (MS, SHEL, GE, BRK-B, WFC) × both
    builders are now publishing cleanly. Total recovery cost across
    the session: ~$3 in Anthropic credits for diagnostic + publish
    rounds.

- **`fundamentals` block added to consensus_snapshots.** New jsonb
  column populated by `consensus_builder.py`:
  - `fiscal_year_end` (date)
  - `growth`: revenue_yoy_pct, eps_yoy_pct, revenue_5y_cagr_pct,
    eps_5y_cagr_pct (5y fields null for nearly all — yfinance only
    returns 4 annual datapoints)
  - `valuation`: forward_pe_fy0, forward_pe_fy1 (both from
    earnings_estimate so they share a fiscal-year basis),
    price_to_fcf_ttm, ev_to_ebitda_ttm
  - `non_gaap_eps_ttm`: sum of last 4 earnings_cards.card.results.
    eps_actual; null until cards accumulate (~3 quarters out for
    most tickers)
  - `eps_beat_history`: last 4 quarters from yfinance.earnings_history,
    surprise normalised from fraction to percent
  - Schema migration applied (`alter table consensus_snapshots add
    column if not exists fundamentals jsonb`). Backfilled all 33
    tickers at no cost (yfinance only).
  - Forward FCF / forward EBITDA intentionally out of scope per v1
    spec — would require a paid data source. Only forward P/E
    extends past TTM.

- **Validation tool**: `validate_fundamentals.py` cross-checks every
  fundamental field against SEC EDGAR's XBRL `companyfacts` API
  (the most authoritative free source).
  - fiscal_year_end: 10/10 perfect match
  - quarterly revenue/EPS: 4/10 within 5% of EDGAR. Remaining DIFFs
    are explainable, NOT data quality issues — Q4 derived from
    10-K minus Q1+Q2+Q3, yfinance one quarter stale, banks not
    normalised. Module docstring lays out the categories.
  - Forward-looking fields (forward_pe_*, P/FCF, EV/EBITDA) and
    non_gaap_eps_ttm are not directly validatable against free
    public sources.
  - Recommended re-run cadence: quarterly after each consensus
    backfill, manually after any schema or builder changes.

- **Lovable UI updates needed** (paste-pending — user task):
  - On `/companies/:ticker`, render the new `fundamentals` block
    grouped as Header (fiscal_year_end), Growth Rates, Valuation
    Multiples, Recent EPS Beats. Hide null-value rows; for
    financials with all-null FCF/EBITDA show single line
    "Cash-flow and EBITDA multiples not applicable for financials".
  - Label the EPS Beats table heading "Recent EPS Beats (GAAP)"
    with subtitle clarifying the difference vs the non-GAAP
    figures shown in the per-quarter earnings cards on the same
    page.

## Currently in progress

- **Manual Supabase SQL migration outstanding**: the `weekly_wraps`
  table needs to be created in the Supabase SQL Editor before the
  first Friday cron fires (the `create table if not exists` block at
  the bottom of `schema.sql`). Until then `run_weekly.py` will save
  locally but the publish step will 404. Lovable also needs a renderer
  for the weekly-wrap row when the user is ready.

## Next session (Monday 2026-04-27)

Highest priority items first.

1. **Check Mon 04:30 UTC GH Actions run landed cleanly.**
   - Verify a new row exists in `digests` with date = `2026-04-24`
     (Friday session) since this is the first run after the weekend.
   - Inspect `digest.meta.warnings` for any parse failures from the
     `digest_writer.py` retry/dump fix (CBRE and similar).
   - If anything failed, `output/failed_parses_*.jsonl` should have
     raw model outputs to diagnose.
2. **User pastes pending Lovable prompts** (blocker for the UI to
   show everything we've built):
   - Phase 2b prompt — Risk Profile + Upcoming Catalysts sections on
     `/companies/:ticker`. Prompt lives in a message from Friday.
   - Phase 2c prompt — Consensus + Macro Sensitivities sections on
     the same page. Prompt also in a Friday message.
3. **Digest heading UX** — user flagged that "Digest for Thu 23 April"
   reads stale on Friday morning. Lovable prompt for the fix was
   written Friday; user just needs to paste it.
4. **Clean BLK duplicate** in `earnings_cards`. Direct SQL:
   `delete from earnings_cards where ticker='BLK' and fiscal_period
   like 'Q1 2026 (three%';`

## Rest of next week

### Tue 2026-04-28 — Earnings begin
- **V reports** (after close). Monitor Wed-morning auto-run to
  confirm the card lands with the writer fix applied.
- If time: investigate why `GE/RTX/AXP/CBRE` earnings cards didn't
  auto-generate from this past week's prints. Suspect lookback
  window (36h) missed some Tuesday-morning filings. Widening to
  48h might fix.

### Wed 2026-04-29 — Peak earnings day
- **MSFT, META, AMZN, GOOGL** all report within 24h. All after-close
  US time, so cards should appear in Thu morning's 06:30 UTC cron run.
- Manual midday trigger (`run_daily.py --now` or earnings-only
  pipeline) can pull cards immediately after each filing rather than
  waiting for the next day's cron.
- Budget 45-60 min for any parse or extraction bugs live traffic
  surfaces on these specific filers.

### Thu 2026-04-30 — AAPL
- AAPL reports after close.
- If there's slack, close Phase 2a failures: MS uses column-table
  TOC, SHEL files 20-F, GE uses non-standard body headings, BRK-B
  unusual, WFC intermittent. A day of work for 15% watchlist payoff
  — skip if live-earnings takes priority.

### Fri 2026-05-01 — Retro + next-phase decision
- What landed, what's still rough, what next week's priority is.
- **Phase 2d decision**: house views (user writes theses → Ascension
  View overlay on cards), or transcripts (Motley Fool scrape → real
  guidance quality), or something else. Decision point for the user.

## Week-at-a-glance (detail above)

| Day | Focus |
|---|---|
| Mon | Check cron. Paste 2 Lovable prompts. Fix digest heading. Clean BLK. |
| Tue | V earnings. Maybe widen earnings lookback to 48h. |
| Wed | **MSFT/META/AMZN/GOOGL report** — live pipeline watch. |
| Thu | AAPL reports. Close 2a/2b profile failures if time. |
| Fri | Retro. Phase 2d decision (house views vs transcripts). |

## Known issues to tackle when time allows

- **Earnings-card capture gaps this week**: GE, RTX (Apr 21), AXP, CBRE
  (Apr 23) reported but no card in `earnings_cards`. Investigate:
  either 8-K lookback window missed them, or the 8-K didn't include
  Item 2.02, or the EX-99.1 naming convention wasn't matched.
  Lower-priority — can wait.
- **Company/risk profile failures for MS, SHEL, GE, BRK-B, WFC**: 10-K
  format quirks. `risk_profile_builder.py` uses a reasonably robust
  body-detector now but these 5 still fail. MS uses a column-table TOC;
  SHEL files 20-F not 10-K; BRK-B has unusual structure. Custom
  handlers are a day of work for a 15%-of-watchlist payoff. Park unless
  prioritised.
- **BLK duplicate earnings_cards row**: two rows with different
  `fiscal_period` strings ("Q1 2026" and the older verbose
  "Q1 2026 (three months ended March 31, 2026)"). Clean up with a
  direct DELETE when convenient.
- **Lovable Risk + Catalysts sections**: prompt was written but not yet
  pasted into Lovable. Do this whenever the user is ready. Prompt lives
  in the conversation from the Phase 2b shipping session.
- **Digest heading UX**: user flagged that "Digest for Thu 23 April"
  reads stale on Friday morning. Lovable prompt for the fix was written
  but not yet pasted.
- **GitHub Actions cron drift**: scheduled 04:30 UTC but lands ~06:30
  UTC due to GH queue delays. Not blocking — still hits before US open.
  If timing matters later, options are (a) schedule earlier (02:00
  UTC) or (b) fire multiple crons.

## Parked features (roadmap, not scheduled)

- **House views overlay** — requires user-written thesis per ticker.
  Table + admin form in Lovable. Adds "Ascension View" block to
  earnings cards and company pages. Highest ROI when user is ready to
  write the content.
- **Transcript ingestion** — FMP Ultimate ($79/mo) or Motley Fool
  scrape (free, 1-2 days engineering). Unlocks guidance quality on
  earnings cards. Decision parked until user picks a path.
- **Phase 2d features** — event memory (prior surprise patterns),
  thesis evolution tracking, event-driven alerting (Slack/email on
  guidance cuts, exec departures, >5% price moves). Parked
  indefinitely pending Phase 2c completion.

## Long-term / strategic

- **Sellable product?** Honest take in conversation: current state is a
  strong internal tool for Ascension Partners but not saleable without
  (a) niche positioning, (b) human editorial layer, (c) proprietary
  signal beyond LLM summaries, (d) commercial wrapper
  (pricing/legal/support). If taken seriously: 3-6 months of work on
  top of current foundation.

## How to keep this file useful

- **Don't bloat it** — long parked lists belong at the bottom, not
  cluttering "currently in progress".
- **Reconcile at session end** — when you close a work slot, update
  "Currently in progress" and "Next session" so the next session picks
  up cleanly.
- **Link issues to real tickers/files** — instead of "fix earnings
  bug", write "fix `earnings_backend.py` lookback window; see `GE`
  missed capture Apr 21".
- **Mirror significant `TodoWrite` changes** — the todo tool is
  in-session only, so persistent work tracking lives here.
