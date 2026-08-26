-- ============================================================================
-- [P-38] Make TOAST compression actually apply
-- Design: zerodha-trading/docs/superpowers/specs/2026-08-27-storage-toast-compression-design.md
-- Verify: VERIFY.md V-15
--
-- WHY: PostgreSQL compresses a value only when the whole row exceeds
-- toast_tuple_target (default 2032 B). Only 92 of 32,145 brain_decisions rows
-- cross that line, so `indicators` -- 1,100 B of a 1,344 B row, ~36% of the
-- entire database -- has been stored raw since day one.
--
-- Measured on production samples: pglz saves 42.0% on brain_decisions and
-- 41.6% on portfolio_advice. (lz4 saves only 18.3% on this data -- small,
-- key-repetitive blobs favour pglz -- so the default codec is NOT changed.)
--
-- Expected: database 114 MB -> ~95.3 MB (-16.4%), growth 7.88 -> 6.51
-- MB/session (-17.4%), runway 49 -> 62 sessions (+27%).
--
-- ⚠️ RUN POST-CLOSE ONLY. VACUUM FULL takes an ACCESS EXCLUSIVE lock and will
--    block the brain if a session is live. Seconds at this size.
--
-- ⚠️ VACUUM FULL CANNOT RUN INSIDE A TRANSACTION BLOCK. Do not wrap this file
--    in BEGIN/COMMIT, and run the VACUUM statements INDIVIDUALLY rather than
--    pasting the whole file as one batch.
-- ============================================================================


-- ── STEP 1 · capture BEFORE (save this output for V-15) ─────────────────────
select c.relname,
       pg_size_pretty(pg_total_relation_size(c.oid))                        total,
       pg_size_pretty(coalesce(pg_total_relation_size(c.reltoastrelid), 0)) toast,
       (select round(avg(pg_column_size(indicators))) from brain_decisions)  avg_ind_decisions,
       (select round(avg(pg_column_size(indicators))) from portfolio_advice) avg_ind_advice
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public'
  and c.relname in ('brain_decisions', 'portfolio_advice');


-- ── STEP 2 · brain_decisions ────────────────────────────────────────────────
-- STORAGE MAIN = compress, but keep inline unless there is no alternative.
-- target 1400 is derived from the measured row distribution (p50 1,632 /
-- p90 1,848 / p99 2,008): a full row compresses to ~1,050-1,380 B and lands
-- JUST UNDER 1400, so it stays inline. A lower target (256/1024) would push
-- `indicators` OUT-OF-LINE -- an extra fetch on every read, and the labelling
-- pass and edge study both read these rows in bulk.
ALTER TABLE public.brain_decisions ALTER COLUMN indicators SET STORAGE MAIN;
ALTER TABLE public.brain_decisions SET (toast_tuple_target = 1400);


-- ── STEP 3 · portfolio_advice ───────────────────────────────────────────────
-- Rows here already exceed 2032, but TOAST stops as soon as the row fits: it
-- shrinks something small and leaves `indicators` raw at 1,030 B. A lower
-- target forces it to compress the columns that actually matter.
ALTER TABLE public.portfolio_advice ALTER COLUMN indicators SET STORAGE MAIN;
ALTER TABLE public.portfolio_advice ALTER COLUMN reasons    SET STORAGE MAIN;
ALTER TABLE public.portfolio_advice SET (toast_tuple_target = 1200);


-- ── STEP 4 · rewrite existing rows ──────────────────────────────────────────
-- The DDL above governs FUTURE writes only. VACUUM FULL rewrites every tuple
-- with the new settings, and reclaims dead-tuple bloat while it is at it.
-- RUN THESE FOUR STATEMENTS ONE AT A TIME.
VACUUM FULL public.brain_decisions;
ANALYZE public.brain_decisions;
VACUUM FULL public.portfolio_advice;
ANALYZE public.portfolio_advice;


-- ── STEP 5 · capture AFTER (re-run STEP 1) ──────────────────────────────────
-- PASS  = avg_ind_decisions drops 1,100 -> <= 700
--         AND brain_decisions total 52 MB -> <= 40 MB
-- GUARD = brain_decisions `toast` stays < 5 MB. If it is tens of MB, values
--         went out-of-line: the target is too low. Roll back and raise it.


-- ── ROLLBACK (only if the guard above trips) ────────────────────────────────
-- Nothing is lost either way; this only changes how bytes are stored.
--
--   ALTER TABLE public.brain_decisions  RESET (toast_tuple_target);
--   ALTER TABLE public.brain_decisions  ALTER COLUMN indicators SET STORAGE EXTENDED;
--   ALTER TABLE public.portfolio_advice RESET (toast_tuple_target);
--   ALTER TABLE public.portfolio_advice ALTER COLUMN indicators SET STORAGE EXTENDED;
--   ALTER TABLE public.portfolio_advice ALTER COLUMN reasons    SET STORAGE EXTENDED;
--   VACUUM FULL public.brain_decisions;
--   VACUUM FULL public.portfolio_advice;
