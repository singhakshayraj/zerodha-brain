-- [P-68]/[P-71] 2026-11-19: the book CHECK on both paper tables only allowed
-- MANAGEMENT and PICKING, so every PICKING_RP write since 11-18 was rejected
-- (and swallowed). Widen to the four books in use. Keep in sync with
-- tests/fake_supabase.py PAPER_BOOKS.
alter table public.advisor_paper_positions drop constraint advisor_paper_positions_book_check;
alter table public.advisor_paper_positions add constraint advisor_paper_positions_book_check
  check (book = any (array['MANAGEMENT','PICKING','PICKING_RP','S5']));
alter table public.advisor_paper_equity drop constraint advisor_paper_equity_book_check;
alter table public.advisor_paper_equity add constraint advisor_paper_equity_book_check
  check (book = any (array['MANAGEMENT','PICKING','PICKING_RP','S5']));
