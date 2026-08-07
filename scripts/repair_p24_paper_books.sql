-- P-24 repair: de-duplicate the 2026-08-06 MANAGEMENT seed's realized P&L.
--
-- The seed booked 7 rotated-out holdings TWICE — a full SELL_VERDICT close
-- (whose qty was then zeroed by the rotation leg's qty update) and a full
-- ROTATION_OUT close with identical entry, exit and realized_pnl. The 16 closed
-- SEED rows sum to -71,512.79; the 9 distinct names sum to -39,983.84, and the
-- 7 duplicate pairs sum to exactly -31,528.95, the difference to the rupee.
--
-- The code fix is brain `advisor_paper._apply_management` (P-24): `remaining`
-- now tracks shares still held across the SELL / TRIM / rotation legs, so each
-- share is realized once. This script repairs the rows already written.
--
-- Shape chosen to match what the fixed code would now produce: keep the
-- original seeded row closed as SELL_VERDICT carrying the real closed qty, drop
-- the ROTATION_OUT duplicate. The rotation BUY rows (source='ROTATION') and the
-- TRIM rows are CORRECT and must not be touched — ITC 40 open + 40 closed and
-- SILVERBEES 213 open + 212 closed are honest half-trims of 80 and 425.
--
-- Run against prod gilmuwmtdpjccibfhqtx. Verify with the SELECT at the bottom.
--
-- ⚠️ THE EXPECTED TOTALS MOVE. This book grows as new advice closes out, so any
-- figure hard-coded here goes stale. Written 08-06 the target was 9 rows /
-- -39,983.84 (book was 16 rows / -71,512.79). By 08-07 close the book was
-- 18 rows / -77,325.36, making the target 11 rows / -45,796.41.
-- What is INVARIANT is the duplicate sum: the 7 pairs total -31,528.95, and
-- the repair removes exactly one copy of each.
--
-- So DERIVE the target immediately before running, don't trust a constant:
--   select count(*) rows_now, sum(realized_pnl) total_now
--     from advisor_paper_positions
--    where book='MANAGEMENT' and source='SEED' and is_open=false;
--   -- expect after repair: rows_now - 7, and total_now + 31528.95
--
-- The dedup itself is self-limiting regardless: step 2 only deletes rows that
-- pair exactly on symbol+entry+exit+pnl+qty, so a grown book cannot widen it.

begin;

-- 1. give the surviving SELL_VERDICT row its real closed quantity
update advisor_paper_positions s
set qty = r.qty
from advisor_paper_positions r
where s.book = 'MANAGEMENT' and s.qty = 0 and s.exit_reason = 'SELL_VERDICT'
  and r.exit_reason = 'ROTATION_OUT' and r.symbol = s.symbol and r.book = s.book
  and r.entry_price = s.entry_price and r.exit_price = s.exit_price
  and r.realized_pnl = s.realized_pnl;

-- 2. drop the duplicate ROTATION_OUT close (same name, same entry, same exit,
--    same P&L as a SELL_VERDICT row from the same seed)
delete from advisor_paper_positions r
using advisor_paper_positions s
where r.exit_reason = 'ROTATION_OUT' and s.exit_reason = 'SELL_VERDICT'
  and r.book = 'MANAGEMENT' and s.book = 'MANAGEMENT'
  and r.symbol = s.symbol and r.source = s.source
  and r.entry_price = s.entry_price and r.exit_price = s.exit_price
  and r.realized_pnl = s.realized_pnl
  and r.qty = s.qty;

commit;

-- verification: compare against the pre-run numbers you derived above —
-- rows should drop by exactly 7 and the total should rise by exactly 31528.95.
select count(*) closed_rows, sum(realized_pnl) total
from advisor_paper_positions
where book = 'MANAGEMENT' and source = 'SEED' and is_open = false;
-- as of 2026-08-07 close that means: 11 | -45796.41

-- and this must return zero rows afterwards (VERIFY I-1)
select symbol, entry_price, exit_price, realized_pnl, count(*) n
from advisor_paper_positions
where book = 'MANAGEMENT' and is_open = false
group by 1,2,3,4 having count(*) > 1;
