# PLAN.md

Living roadmap of work on this project. Read this at the start of every
session. Update it whenever you update `TodoWrite` with meaningful status
changes — otherwise the in-session todos vanish and future sessions lose
context.

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
  hypothesis (lookback window too short) was wrong for AXP/CBRE — they
  filed Apr 23 11:00 UTC, well within the 36h window when the Apr 24
  cron ran at 06:25 UTC. Real root cause was the writer's missing
  retry. Hypothesis still open for GE/RTX (Apr 21) — they don't
  appear in a 240h backend run, suggesting backend can't even bundle
  them. Next investigation: check whether their 8-K includes Item
  2.02 and whether EX-99.1 naming matches the script's filter.

## Currently in progress

_Nothing. Phase 2b and Phase 2c shipped 2026-04-24; out-of-band
maintenance completed 2026-04-30 (see above)._

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
