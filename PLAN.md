# PLAN.md

Living roadmap of work on this project. Read this at the start of every
session. Update it whenever you update `TodoWrite` with meaningful status
changes — otherwise the in-session todos vanish and future sessions lose
context.

Last updated: 2026-04-24 (Friday)

## Currently in progress

_Nothing — Phase 2b shipped today. Weekend is idle._

## Next session (Monday 2026-04-27)

1. **Check Mon 04:30 UTC GH Actions run** — first automated run since the
   `digest_writer.py` retry/dump fix landed. Verify no parse failures
   (check `digest.meta.warnings` in the published row).
2. **Phase 2c: macro sensitivities** — build
   `sensitivity_builder.py`. Pure Python/pandas: pull 3 years of daily
   returns from yfinance per ticker, compute rolling correlations vs
   FRED macro series (10Y, DXY, Brent, gold, CPI change, HY spread,
   VIX). New `macro_sensitivities` Supabase table. One new section on
   `/companies/:ticker`. Estimated ~2 hours.
3. **Sketch a Lovable prompt** for the Macro Sensitivities section.

## This week's plan (the original Tue/Wed/Thu/Fri has shifted by a week)

| Day | Focus | Status |
|---|---|---|
| Mon 2026-04-27 | Phase 2c: macro sensitivities | pending |
| Tue 2026-04-28 | Phase 2c: consensus framework (`consensus_builder.py`, weekly snapshots) | pending |
| Wed 2026-04-29 | **Live earnings day** — MSFT, META, AMZN, GOOGL all report. Monitor pipeline, debug any parse/fetch issues. | pending |
| Thu 2026-04-30 | **Live earnings** — AAPL reports. Polish + close Phase 2a/2b gaps. | pending |
| Fri 2026-05-01 | Week retro. Decide next focus. | pending |

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
