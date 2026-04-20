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
