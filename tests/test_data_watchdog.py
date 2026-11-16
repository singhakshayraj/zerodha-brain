"""[P-56] Watchdog: each check names the failing number and a cause."""
import json
from unittest.mock import patch

import data_watchdog as wd
from tests.fake_supabase import FakeSupabase

T = '2026-11-16'      # Monday; prev trading day 11-13


def _fake(**over):
    f = FakeSupabase()
    held = ['ATGL', 'INFY']
    f.tables['portfolio_advice'] = [{'symbol': s, 'verdict': 'HOLD', 'run_date': T, 'is_official': True,
                                     'rotation_target_symbol': None, 'evaluated_at': None} for s in held]
    f.tables['universe_calls'] = [{'symbol': f'U{i}', 'run_date': T, 'evaluated_at': None} for i in range(450)]
    f.tables['account_calls'] = []
    f.tables['candles'] = [{'symbol': s, 'interval': 'day', 'trade_date': '2026-11-13'} for s in held]
    f.tables['macro_daily'] = [{'date': '2026-11-13', 'data': {'nifty': {'close': 1}, 'vix': {'close': 1},
                                                               'usdinr': {'close': 1}, 'crude': {'close': 1}, 'fii_net_cr': 100}}]
    f.tables['pnl_daily'] = [{'account': 'primary', 'date': T, 'pnl': 1500, 'missing_prices': 0}]
    f.tables['app_config'] = [{'key': 'fundamentals_latest', 'value': json.dumps({'asof': '2026-11-01'})},
                              {'key': 'morning_brief_latest', 'value': json.dumps({'run_date': T})}]
    for k, v in over.items():
        f.tables[k] = v
    return f


def _status(checks):
    return {c['name']: c['status'] for c in checks}


def test_clean_day_passes_everything():
    f = _fake()
    with patch.object(wd.db, 'supabase', f):
        out = wd.run(T)
    assert out['fails'] == 0 and out['warns'] == 0, _status(out['checks'])
    assert any(r['key'] == wd.KEY for r in f.tables['app_config'])


def test_price_gap_and_etf_rotation_and_missing_pnl_fail_with_causes():
    f = _fake(candles=[{'symbol': 'ATGL', 'interval': 'day', 'trade_date': '2026-11-13'}], pnl_daily=[])
    f.tables['portfolio_advice'].append({'symbol': 'GOLDBEES', 'verdict': 'SELL', 'run_date': T, 'is_official': True,
                                         'rotation_target_symbol': 'PVRINOX', 'evaluated_at': None})
    with patch.object(wd.db, 'supabase', f):
        checks = wd.run_checks(T)
    st = _status(checks)
    assert st['bars_fresh'] == 'FAIL' and st['etf_not_rotated'] == 'FAIL' and st['pnl_primary'] == 'FAIL'
    bars = next(c for c in checks if c['name'] == 'bars_fresh')
    assert '2/3 held names missing the 2026-11-13 bar' in bars['evidence'] and 'GOLDBEES, INFY' in bars['evidence']
    assert bars['cause']


def test_natwar_checks_only_when_his_token_was_pasted_today():
    f = _fake(account_calls=[{'symbol': 'SCI', 'verdict': 'SELL', 'run_date': T, 'rotation_target_symbol': None}])
    f.tables['app_config'].append({'key': 'enc_token_natwar', 'value': 'x', 'updated_at': T + 'T06:41:00+00:00'})
    f.tables['candles'].append({'symbol': 'SCI', 'interval': 'day', 'trade_date': '2026-11-13'})
    with patch.object(wd.db, 'supabase', f):
        st = _status(wd.run_checks(T))
    assert st['natwar_calls'] == 'PASS' and st['natwar_targets'] == 'WARN' and st['pnl_natwar'] == 'FAIL'
    with patch.object(wd.db, 'supabase', _fake()):
        st = _status(wd.run_checks(T))
    assert st['natwar_calls'] == 'PASS' and 'natwar_targets' not in st and 'pnl_natwar' not in st


def test_stuck_grading_and_old_fundamentals():
    f = _fake(app_config=[{'key': 'fundamentals_latest', 'value': json.dumps({'asof': '2026-09-01'})}])
    f.tables['universe_calls'].append({'symbol': 'OLD', 'run_date': '2026-10-01', 'evaluated_at': None})
    with patch.object(wd.db, 'supabase', f):
        st = _status(wd.run_checks(T))
    assert st['grading_moves'] == 'FAIL' and st['fundamentals_age'] == 'WARN' and st['brief_today'] == 'WARN'


def test_brief_carries_last_sessions_failures():
    import morning_brief as mb
    wdog = {'run_date': '2026-11-13', 'checks': [{'name': 'bars_fresh', 'status': 'FAIL', 'evidence': '2/30 missing'},
                                                 {'name': 'fii_row', 'status': 'WARN', 'evidence': 'fii_net_cr None'}]}
    lines = mb.compose(T, [], [], None, None, {}, None, None, wdog)
    watch = next(l for l in lines if l['kind'] == 'watch')
    assert watch['warn'] and watch['text'] == 'Last session (2026-11-13): FAIL bars_fresh — 2/30 missing; WARN fii_row — fii_net_cr None'
    assert not [l for l in mb.compose(T, [], [], None, None, {}, None, None, {**wdog, 'run_date': '2026-11-10'}) if l['kind'] == 'watch']


def test_bar_integrity_warns_on_an_unexplained_halving():
    f = _fake(candles=[{'symbol': 'ATGL', 'interval': 'day', 'trade_date': '2026-11-12', 'close': 100.0},
                       {'symbol': 'ATGL', 'interval': 'day', 'trade_date': '2026-11-13', 'close': 48.0},
                       {'symbol': 'INFY', 'interval': 'day', 'trade_date': '2026-11-13', 'close': 10.0}])
    with patch.object(wd.db, 'supabase', f), patch.object(wd.data_jobs.db, 'supabase', f):
        out = wd.run(T)
    c = next(c for c in out['checks'] if c['name'] == 'bar_integrity')
    assert c['status'] == 'WARN' and 'ATGL 0.48' in c['evidence']
