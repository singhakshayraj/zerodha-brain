-- ============================================================================
-- [Advisor items 3 & 4] Whole-book pricing: AMFI NAV + hand-curated mapping
-- Reviewable DDL — apply by hand, never by an agent. Idempotent.
--
-- amfi_nav: the daily public NAV feed (scheduler ingests it, no token).
-- holding_scheme_map: HAND-CURATED. One row per NAV-based holding, mapping it
--   to its AMFI scheme code, its sleeve, and the units held. Never
--   auto-populated: near-identical names across Direct/Regular and Growth/IDCW
--   variants make a wrong match silently misprice the largest block of the
--   book, so every row is confirmed by the operator.
-- ============================================================================

-- 1. AMFI NAV feed --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.amfi_nav (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scheme_code  text        NOT NULL,
    scheme_name  text        NOT NULL,
    nav          numeric     NOT NULL,
    nav_date     date        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scheme_code, nav_date)          -- idempotent re-ingest
);
CREATE INDEX IF NOT EXISTS amfi_nav_code_date_idx
    ON public.amfi_nav (scheme_code, nav_date DESC);

-- 2. Hand-curated holding -> scheme mapping ------------------------------------
CREATE TABLE IF NOT EXISTS public.holding_scheme_map (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    holding_label     text NOT NULL UNIQUE,          -- operator's name for it
    amfi_scheme_code  text NOT NULL,                 -- confirmed AMFI code
    sleeve            text NOT NULL
        CHECK (sleeve IN ('equity','precious_metals','debt_cash','other')),
    units             numeric NOT NULL DEFAULT 0,    -- held units (units x NAV = value)
    confirmed         boolean NOT NULL DEFAULT false, -- operator has verified the code
    note              text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- 3. The three NAMED precious-metals FoFs, pre-seeded UNCONFIRMED. The operator
--    must fill amfi_scheme_code from the candidate list the ingest job produces,
--    set units, and flip confirmed=true. The "other mutual funds" in the book
--    are not listed here because they were not supplied -- add a row each.
INSERT INTO public.holding_scheme_map (holding_label, amfi_scheme_code, sleeve, units, confirmed, note)
VALUES
  ('DSP Silver ETF FoF',            'TODO', 'precious_metals', 0, false, 'confirm code + units'),
  ('ICICI Prudential Silver ETF FoF','TODO', 'precious_metals', 0, false, 'confirm code + units'),
  ('Motilal Oswal Gold & Silver FoF','TODO', 'precious_metals', 0, false, 'confirm code + units')
ON CONFLICT (holding_label) DO NOTHING;

-- NOTE: item 4 (target sleeve weights + band widths) is a SEPARATE policy table,
-- not created here -- it is blocked on the operator supplying the numbers. Do
-- not infer targets from current weights (that makes every drift zero).
