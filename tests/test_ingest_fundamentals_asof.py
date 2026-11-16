"""[P-68 phase 4] `scripts/ingest_fundamentals.py --asof YYYY-MM-DD paste.txt`:
the snapshot date is the export date, not the day it was pasted."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import ingest_fundamentals as ing


def test_asof_flag_is_taken_out_of_the_paths():
    asof, paths = ing.asof_from_args(['--asof', '2026-11-11', 'a.txt', 'b.txt'])
    assert asof == '2026-11-11' and paths == ['a.txt', 'b.txt']


def test_asof_defaults_to_today():
    from datetime import date
    asof, paths = ing.asof_from_args(['a.txt'])
    assert asof == date.today().isoformat() and paths == ['a.txt']
