"""Bar integrity + corporate-action patch list [P-68 phase 0]."""
from unittest.mock import patch
import data_jobs
import call_scoring as cs
import corporate_actions as ca
from tests.fake_supabase import FakeSupabase


def _fake(rows):
    f = FakeSupabase()
    f.tables['candles'] = [{'symbol': s, 'interval': 'day', 'trade_date': d, 'close': c} for s, d, c in rows]
    return f


def test_integrity_flags_a_halving():
    f = _fake([('AAA', '2026-11-13', 100), ('AAA', '2026-11-14', 49),
               ('BBB', '2026-11-13', 100), ('BBB', '2026-11-14', 101)])
    with patch.object(data_jobs.db, 'supabase', f), patch.object(ca, 'load', lambda: []):
        out = data_jobs.check_bar_integrity('2026-11-14')
    assert out == [{'symbol': 'AAA', 'date': '2026-11-14', 'ratio': 0.49}]


def test_known_action_is_not_flagged():
    f = _fake([('AAA', '2026-11-13', 100), ('AAA', '2026-11-14', 49)])
    known = [{'symbol': 'AAA', 'date': '2026-11-14', 'factor': 0.49, 'kind': 'demerger', 'note': ''}]
    with patch.object(data_jobs.db, 'supabase', f), patch.object(ca, 'load', lambda: known):
        assert data_jobs.check_bar_integrity('2026-11-14') == []


def test_scoring_adjusts_across_a_listed_action():
    days = ['2026-11-10', '2026-11-11', '2026-11-12']
    bars = {'NIFTY 50': {d: 100 for d in days}, 'AAA': {'2026-11-10': 100, '2026-11-11': 50, '2026-11-12': 51}}
    known = [{'symbol': 'AAA', 'date': '2026-11-11', 'factor': 0.5, 'kind': 'demerger', 'note': ''}]
    adj = ca.adjust(bars, known)
    assert adj['AAA'] == {'2026-11-10': 50.0, '2026-11-11': 50, '2026-11-12': 51}   # pre-action closes scaled
    call = {'source': 'advice', 'account': 'primary', 'run_date': '2026-11-10', 'symbol': 'AAA',
            'entry': 100.0, 'direction': 1, 'target': None, 'target_entry': None}
    rows = cs.score_call(call, adj, days, entry_adjust=ca.entry_factor('AAA', '2026-11-10', known))
    assert rows[0]['ret_pct'] == 0.0 and rows[1]['ret_pct'] == 2.0
