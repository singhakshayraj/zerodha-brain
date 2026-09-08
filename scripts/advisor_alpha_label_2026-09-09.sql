-- ============================================================================
-- [Advisor item 2] Market-neutral correctness label
-- Run by hand (reviewable DDL, not applied by an agent). Apply BEFORE the
-- brain redeploys the code that writes/reads outcome_correct_alpha, or the
-- grader's UPDATE will error on a missing column.
--
-- WHY: outcome_correct grades HOLD as right whenever the stock ROSE and an
-- exit as right whenever it FELL -- absolute return, so in a rising market
-- every HOLD scores correct regardless of skill. The market-neutral column
-- outcome_vs_nifty_pct is already computed and stored; this promotes it to the
-- authoritative label. The absolute column is RETAINED (house rule: reversals
-- are recorded, not overwritten), marked superseded below.
--
-- Verified before shipping: recomputed over the 98 originally-graded calls,
-- the alpha label reproduces hit rate 0.5510 and AUC 0.5133 (vs absolute's
-- 0.480 / 0.4917) -- the item-2 regression numbers, to four decimals.
-- ============================================================================

ALTER TABLE public.portfolio_advice
    ADD COLUMN IF NOT EXISTS outcome_correct_alpha boolean;

COMMENT ON COLUMN public.portfolio_advice.outcome_correct IS
  'SUPERSEDED 2026-09-09 by outcome_correct_alpha. Absolute-return label: HOLD '
  'correct if the stock rose, exit correct if it fell. Confounded by market '
  'direction. Kept for legibility of the reversal; do NOT read for correctness.';

COMMENT ON COLUMN public.portfolio_advice.outcome_correct_alpha IS
  'Authoritative correctness label (2026-09-09). Market-neutral: HOLD correct '
  'if outcome_vs_nifty_pct > 0; exit (SELL/SELL_ON_BOUNCE/TRIM) correct if < 0; '
  'NULL for INSUFFICIENT/unknown verdicts or NULL alpha.';

-- Backfill every historical graded row from the already-stored alpha column.
UPDATE public.portfolio_advice
SET outcome_correct_alpha = CASE
        WHEN outcome_vs_nifty_pct IS NULL THEN NULL
        WHEN verdict = 'HOLD' THEN (outcome_vs_nifty_pct > 0)
        WHEN verdict IN ('SELL', 'SELL_ON_BOUNCE', 'TRIM')
             THEN (outcome_vs_nifty_pct < 0)
        ELSE NULL
    END
WHERE outcome_correct IS NOT NULL;

-- Verify after running: on the 98 rows evaluated on/before 2026-08-24 this must
-- read hit rate 0.5510. (Newer rows have since matured; the full population is
-- larger and reads higher -- that is accrual, not a regression.)
--   select round(avg((outcome_correct_alpha)::int)::numeric,4) alpha_hit_rate,
--          count(*) n
--   from portfolio_advice
--   where outcome_correct is not null and outcome_correct_alpha is not null
--     and evaluated_at::date <= '2026-08-24';
