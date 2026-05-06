-- Schema for the Morning Digest Supabase project.
-- Run in the Supabase SQL editor (Project > SQL Editor > New query).

-- Existing: digests (Lovable reads from this).
-- Documented here for completeness; table is already created.
--
-- create table if not exists digests (
--   date         date primary key,
--   generated_at timestamptz not null,
--   digest       jsonb       not null,
--   meta         jsonb       not null,
--   created_at   timestamptz not null default now()
-- );

-- New: one row per company per fiscal period. Stores the guidance block
-- extracted from each processed earnings card, so the NEXT quarter's
-- earnings pipeline can load this as `prior_guidance` for the Claude prompt.
-- Backend-only — Lovable never reads this directly, so RLS is enabled with
-- no policies (service role key bypasses RLS; anon key gets nothing).
create table if not exists public.company_guidance (
  ticker        text        not null,
  fiscal_period text        not null,
  guidance      jsonb       not null,
  filed_at      timestamptz not null,
  created_at    timestamptz not null default now(),
  primary key (ticker, fiscal_period)
);

alter table public.company_guidance enable row level security;

create index if not exists company_guidance_ticker_filed_at_idx
  on public.company_guidance (ticker, filed_at desc);

-- One row per company per fiscal period. Lovable reads this directly and
-- filters by filed_at for the "Earnings Today" / "Recent Earnings" sections,
-- so cards display on their actual filing date rather than being conflated
-- with whichever daily pipeline run generated them.
create table if not exists public.earnings_cards (
  ticker        text        not null,
  fiscal_period text        not null,
  filed_at      timestamptz not null,
  card          jsonb       not null,
  created_at    timestamptz not null default now(),
  primary key (ticker, fiscal_period)
);

alter table public.earnings_cards enable row level security;

create policy "public read"
  on public.earnings_cards
  for select
  using (true);

create index if not exists earnings_cards_filed_at_idx
  on public.earnings_cards (filed_at desc);

-- Research-note "library" layer (Phase 2). One row per watchlist ticker.
-- Rebuilt annually from the company's latest 10-K Item 1 (Business).
-- Public read so Lovable's `/companies/:ticker` page can render with the
-- anon key; writes go through the pipeline's service-role key.
create table if not exists public.company_profiles (
  ticker                  text primary key,
  company_name            text not null,
  sector                  text,
  business_description    text,
  revenue_segments        jsonb,
  geographic_exposure     jsonb,
  key_products            jsonb,
  primary_competitors     jsonb,
  hq_location             text,
  employee_count          int,
  website                 text,
  source_filing_url       text,
  source_filing_accession text,
  refreshed_at            timestamptz not null default now()
);

alter table public.company_profiles enable row level security;

create policy "public read"
  on public.company_profiles
  for select
  using (true);

-- Phase 2b risk profiles: distilled summary of Item 1A "Risk Factors" from
-- each company's 10-K. One row per ticker; rebuilt annually after 10-K.
create table if not exists public.risk_profiles (
  ticker                  text primary key,
  risks                   jsonb not null,
  source_filing_url       text,
  source_filing_accession text,
  refreshed_at            timestamptz not null default now()
);

alter table public.risk_profiles enable row level security;

create policy "public read"
  on public.risk_profiles
  for select
  using (true);

-- Phase 2b catalysts: forward-looking events per ticker. Earnings dates
-- refreshed from yfinance each pipeline run; non-earnings events can be
-- inserted manually or by future builders (IR calendar scrapers, etc.).
create table if not exists public.catalysts (
  id          bigserial primary key,
  ticker      text        not null,
  event_date  date        not null,
  event_type  text        not null,           -- earnings | analyst_day | regulatory | product | other
  description text,
  source      text        not null default 'manual',
  created_at  timestamptz not null default now(),
  unique (ticker, event_date, event_type)
);

create index if not exists catalysts_event_date_idx
  on public.catalysts (event_date);

create index if not exists catalysts_ticker_date_idx
  on public.catalysts (ticker, event_date);

alter table public.catalysts enable row level security;

create policy "public read"
  on public.catalysts
  for select
  using (true);

-- Phase 2c macro sensitivities: rolling correlation + beta of each
-- stock's daily returns against a curated set of FRED macro series
-- (yields, FX, commodities, credit spreads, VIX). Computed quarterly.
-- One row per (ticker, series).
create table if not exists public.macro_sensitivities (
  ticker         text        not null,
  series_id      text        not null,
  series_name    text        not null,
  correlation    numeric,
  beta           numeric,
  r_squared      numeric,
  n_observations integer,
  window_days    integer     not null,
  direction      text        not null,
  magnitude      text        not null,
  computed_at    timestamptz not null default now(),
  primary key (ticker, series_id)
);

create index if not exists macro_sensitivities_ticker_idx
  on public.macro_sensitivities (ticker);

alter table public.macro_sensitivities enable row level security;

create policy "public read"
  on public.macro_sensitivities
  for select
  using (true);

-- Phase 2c consensus snapshots: point-in-time captures of analyst
-- estimates, price targets, and recommendation distribution per ticker.
-- Run weekly so revision trends accumulate over time; Lovable reads the
-- most-recent row per ticker for the page, but historical rows enable
-- revision analytics later.
create table if not exists public.consensus_snapshots (
  id                bigserial   primary key,
  ticker            text        not null,
  asof_date         date        not null,
  revenue_estimates jsonb,
  eps_estimates     jsonb,
  price_targets     jsonb,
  recommendations   jsonb,
  -- Added 2026-04-30: trailing growth rates + valuation multiples (forward P/E
  -- FY0/FY1, Price/FCF TTM, EV/EBITDA TTM) + last 8 quarters of EPS beats +
  -- fiscal_year_end. Forward FCF / forward EBITDA require a paid data source
  -- and are intentionally out of scope; only forward P/E goes forward today.
  fundamentals     jsonb,
  created_at        timestamptz not null default now(),
  unique (ticker, asof_date)
);

-- One-shot migration for tables that already exist:
alter table public.consensus_snapshots
  add column if not exists fundamentals jsonb;

create index if not exists consensus_ticker_asof_idx
  on public.consensus_snapshots (ticker, asof_date desc);

alter table public.consensus_snapshots enable row level security;

create policy "public read"
  on public.consensus_snapshots
  for select
  using (true);

-- Phase 2c weekly wraps: Friday close-of-play recap covering Mon-Fri.
-- Mirrors the `digests` shape but `week_ending` (Friday's date) is the PK
-- and `wrap` jsonb holds the recap-shaped editorial output.
create table if not exists public.weekly_wraps (
  week_ending  date primary key,
  generated_at timestamptz not null,
  wrap         jsonb       not null,
  meta         jsonb       not null,
  created_at   timestamptz not null default now()
);

alter table public.weekly_wraps enable row level security;

create policy "public read"
  on public.weekly_wraps
  for select
  using (true);
