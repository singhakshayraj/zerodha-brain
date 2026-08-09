-- ROLLBACK snapshot for the [P-24] repair, captured 2026-08-10 pre-market
-- (~01:15 IST) immediately before running scripts/repair_p24_paper_books.sql
-- against prod gilmuwmtdpjccibfhqtx.
--
-- All 14 rows of the 7 duplicate pairs, exactly as they stood. The repair does
-- two things, and this undoes both:
--   1. sets each SELL_VERDICT row's qty from 0 to the real closed quantity
--   2. deletes the ROTATION_OUT twin
--
-- To restore: run the DELETE, then the INSERT below. That returns the book to
-- 18 closed MANAGEMENT/SEED rows summing to -77,325.36.
--
-- Pre-repair state (derived at run time, matches the documented target):
--   rows_before 18 / total_before -77,325.36
--   rows_after  11 / total_after  -45,796.41
--   duplicate_sum removed          -31,528.95

begin;

-- clear whatever the repair left for these 7 names, then put the originals back
delete from advisor_paper_positions
where id in (
  'da5d1b67-4e00-4e74-9890-31891a849962','621779e2-c933-467e-bf79-304b651da1e6',
  '1dd25b1c-c2ed-405e-a7c4-d5330c98e4bf','129bd76f-e724-454f-9343-ac088f64c9b0',
  'ff1c7634-bed2-4001-ba45-5aaef2bf2707','f0f0962f-7662-450f-8512-0bbba34deab4',
  'a3b0c76c-b711-4bec-a405-a2afd2729cbf','82f9c7a4-89e9-4117-b8aa-45ea74b72631',
  'd079a4a5-3c6a-4244-ac92-67c02d3a1fd9','22cd8f2b-e737-4c01-9b64-3e0a9636f7aa',
  '4f106158-b740-4831-a78e-c15ab7c12e99','65adbd32-176f-4b30-bb3d-850cc466da76',
  '4f56e62c-439a-4313-912d-740486996c41','9a4c54ab-0fd0-4f9b-98b6-69e3dfe0e956'
);

insert into advisor_paper_positions
  (id, book, symbol, source, source_advice_id, verdict, qty, entry_price,
   entry_date, exit_price, exit_date, exit_reason, realized_pnl, return_pct,
   is_open, created_at, updated_at)
values
 ('da5d1b67-4e00-4e74-9890-31891a849962','MANAGEMENT','ATGL','SEED','c1cb6373-dd62-4d20-85a0-fe1f40a992cd','SELL',91,765.451098,'2026-08-06',660.95,'2026-08-06','ROTATION_OUT',-9509.6,-13.652,false,'2026-08-06 04:31:13.411132+00','2026-08-06 04:31:13.411132+00'),
 ('621779e2-c933-467e-bf79-304b651da1e6','MANAGEMENT','ATGL','SEED','c1cb6373-dd62-4d20-85a0-fe1f40a992cd','SELL',0,765.451098,'2026-08-06',660.95,'2026-08-06','SELL_VERDICT',-9509.6,-13.652,false,'2026-08-06 04:31:06.07878+00','2026-08-06 04:31:06.07878+00'),
 ('1dd25b1c-c2ed-405e-a7c4-d5330c98e4bf','MANAGEMENT','IREDA','SEED','d8774557-7de7-4c1b-a719-b0b52bf4b196','SELL',146,132.630684,'2026-08-06',120.72,'2026-08-06','ROTATION_OUT',-1738.96,-8.98,false,'2026-08-06 04:31:13.411132+00','2026-08-06 04:31:13.411132+00'),
 ('129bd76f-e724-454f-9343-ac088f64c9b0','MANAGEMENT','IREDA','SEED','d8774557-7de7-4c1b-a719-b0b52bf4b196','SELL',0,132.630684,'2026-08-06',120.72,'2026-08-06','SELL_VERDICT',-1738.96,-8.98,false,'2026-08-06 04:31:06.07878+00','2026-08-06 04:31:06.07878+00'),
 ('ff1c7634-bed2-4001-ba45-5aaef2bf2707','MANAGEMENT','ITCHOTELS','SEED','2d3d1ee4-a319-4b49-93b2-65239cf4ef5a','SELL',65,221.538335,'2026-08-06',169.5,'2026-08-06','ROTATION_OUT',-3382.49,-23.49,false,'2026-08-06 04:31:13.411132+00','2026-08-06 04:31:13.411132+00'),
 ('f0f0962f-7662-450f-8512-0bbba34deab4','MANAGEMENT','ITCHOTELS','SEED','2d3d1ee4-a319-4b49-93b2-65239cf4ef5a','SELL',0,221.538335,'2026-08-06',169.5,'2026-08-06','SELL_VERDICT',-3382.49,-23.49,false,'2026-08-06 04:31:06.07878+00','2026-08-06 04:31:06.07878+00'),
 ('a3b0c76c-b711-4bec-a405-a2afd2729cbf','MANAGEMENT','MAZDOCK','SEED','fd73008b-dbf8-46fd-860a-ae144a5f5ba6','SELL',15,2629.153333,'2026-08-06',2443.5,'2026-08-06','ROTATION_OUT',-2784.8,-7.061,false,'2026-08-06 04:31:13.411132+00','2026-08-06 04:31:13.411132+00'),
 ('82f9c7a4-89e9-4117-b8aa-45ea74b72631','MANAGEMENT','MAZDOCK','SEED','fd73008b-dbf8-46fd-860a-ae144a5f5ba6','SELL',0,2629.153333,'2026-08-06',2443.5,'2026-08-06','SELL_VERDICT',-2784.8,-7.061,false,'2026-08-06 04:31:06.07878+00','2026-08-06 04:31:06.07878+00'),
 ('d079a4a5-3c6a-4244-ac92-67c02d3a1fd9','MANAGEMENT','NBCC','SEED','2576b1f3-0c8b-4557-b897-37aa370544d5','SELL',115,55.593478,'2026-08-06',96.15,'2026-08-06','ROTATION_OUT',4664.0,72.952,false,'2026-08-06 04:31:13.411132+00','2026-08-06 04:31:13.411132+00'),
 ('22cd8f2b-e737-4c01-9b64-3e0a9636f7aa','MANAGEMENT','NBCC','SEED','2576b1f3-0c8b-4557-b897-37aa370544d5','SELL',0,55.593478,'2026-08-06',96.15,'2026-08-06','SELL_VERDICT',4664.0,72.952,false,'2026-08-06 04:31:06.07878+00','2026-08-06 04:31:06.07878+00'),
 ('4f106158-b740-4831-a78e-c15ab7c12e99','MANAGEMENT','NTPC','SEED','06c81b67-22c0-4084-8216-3214450ace1d','SELL',90,342.023888,'2026-08-06',346.85,'2026-08-06','ROTATION_OUT',434.35,1.411,false,'2026-08-06 04:31:13.411132+00','2026-08-06 04:31:13.411132+00'),
 ('65adbd32-176f-4b30-bb3d-850cc466da76','MANAGEMENT','NTPC','SEED','06c81b67-22c0-4084-8216-3214450ace1d','SELL',0,342.023888,'2026-08-06',346.85,'2026-08-06','SELL_VERDICT',434.35,1.411,false,'2026-08-06 04:31:06.07878+00','2026-08-06 04:31:06.07878+00'),
 ('4f56e62c-439a-4313-912d-740486996c41','MANAGEMENT','RVNL','SEED','4930c067-e775-4777-bed6-49d6ffc2c025','SELL',97,427.916185,'2026-08-06',229.86,'2026-08-06','ROTATION_OUT',-19211.45,-46.284,false,'2026-08-06 04:31:13.411132+00','2026-08-06 04:31:13.411132+00'),
 ('9a4c54ab-0fd0-4f9b-98b6-69e3dfe0e956','MANAGEMENT','RVNL','SEED','4930c067-e775-4777-bed6-49d6ffc2c025','SELL',0,427.916185,'2026-08-06',229.86,'2026-08-06','SELL_VERDICT',-19211.45,-46.284,false,'2026-08-06 04:31:06.07878+00','2026-08-06 04:31:06.07878+00');

commit;

-- confirm the restore
select count(*) closed_rows, sum(realized_pnl) total
from advisor_paper_positions
where book = 'MANAGEMENT' and source = 'SEED' and is_open = false;
-- expect: 18 | -77325.36
