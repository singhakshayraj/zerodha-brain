"""Advisor paper-portfolio data access (P-14 phase 2).

Two virtual books (MANAGEMENT / PICKING) that act on advisor verdicts so we can
measure wins/losses. Advisory-only, no orders. References the shared client on
the `database` module at call time (`database.supabase`) so tests that patch
`database.supabase` still intercept these; database.py re-exports every name so
`db.<name>` callers are unchanged (same facade pattern as db_records/db_stocks).
"""
import database


def paper_positions(book: str, open_only: bool = False) -> list:
    """All positions for a book (optionally only still-open ones)."""
    try:
        q = (database.supabase.table('advisor_paper_positions').select('*')
             .eq('book', book))
        if open_only:
            q = q.eq('is_open', True)
        return q.execute().data or []
    except Exception as e:
        print(f"[paper_positions] {book}: {e}")
        return []


def insert_paper_positions(rows: list) -> int:
    """Bulk-insert new virtual positions. Returns count written."""
    if not rows:
        return 0
    try:
        res = database.supabase.table('advisor_paper_positions').insert(rows).execute()
        return len(res.data or [])
    except Exception as e:
        print(f"[insert_paper_positions] {e}")
        return 0


def update_paper_position(position_id: str, patch: dict) -> bool:
    """Patch one position (e.g. close it: exit_price/pnl/is_open)."""
    try:
        (database.supabase.table('advisor_paper_positions').update(patch)
         .eq('id', position_id).execute())
        return True
    except Exception as e:
        print(f"[update_paper_position] {position_id}: {e}")
        return False


def upsert_paper_equity(row: dict) -> bool:
    """One daily equity snapshot per book (unique on book+snapshot_date), so a
    re-run on the same day overwrites rather than duplicating the curve."""
    try:
        (database.supabase.table('advisor_paper_equity')
         .upsert(row, on_conflict='book,snapshot_date').execute())
        return True
    except Exception as e:
        print(f"[upsert_paper_equity] {row.get('book')}: {e}")
        return False


def paper_equity_curve(book: str, limit: int = 400) -> list:
    """A book's equity snapshots, oldest-first (for the accountability UI)."""
    try:
        res = (database.supabase.table('advisor_paper_equity').select('*')
               .eq('book', book)
               .order('snapshot_date', desc=True).limit(limit).execute())
        return list(reversed(res.data or []))
    except Exception as e:
        print(f"[paper_equity_curve] {book}: {e}")
        return []


def paper_book_exists(book: str) -> bool:
    """Whether a book has been seeded yet (any position row)."""
    try:
        res = (database.supabase.table('advisor_paper_positions')
               .select('id').eq('book', book).limit(1).execute())
        return bool(res.data)
    except Exception as e:
        print(f"[paper_book_exists] {book}: {e}")
        return False
