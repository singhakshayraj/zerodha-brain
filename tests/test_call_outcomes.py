from unittest.mock import patch
import call_outcomes as co
from tests.fake_supabase import FakeSupabase

DAYS = [f'2026-11-{d:02d}' for d in (10, 11, 12, 13, 14)]


def _fake():
    f = FakeSupabase()
    f.tables['candles'] = ([{'symbol': 'NIFTY 50', 'interval': 'day', 'trade_date': d, 'close': 100 + i} for i, d in enumerate(DAYS)]
                           + [{'symbol': 'AAA', 'interval': 'day', 'trade_date': d, 'close': 100 - i} for i, d in enumerate(DAYS)]
                           + [{'symbol': 'BBB', 'interval': 'day', 'trade_date': d, 'close': 100 + 2 * i} for i, d in enumerate(DAYS)])
    f.tables['portfolio_advice'] = [{'symbol': 'AAA', 'verdict': 'SELL', 'run_date': '2026-11-10', 'is_official': True, 'last_price': 100.0,
                                     'rotation_target_symbol': 'BBB', 'rotation_buy_price': 100.0, 'market_regime': 'BULL',
                                     'trigger_type': 'MACRO', 'confidence': 72, 'rotation_reason': 'same_sector', 'indicators': {'thesis': {'action': 'EXIT'}}}]
    f.tables['account_calls'] = [{'account': 'natwar', 'symbol': 'AAA', 'verdict': 'SELL', 'sector_verdict': 'TRIM', 'run_date': '2026-11-10',
                                  'last_price': 100.0, 'regime': 'BULL', 'trigger_type': 'MACRO', 'confidence': 60, 'quality': 55, 'sector_rule': 'strong_sector_softens_sell'}]
    f.tables['universe_calls'] = [{'symbol': 'BBB', 'verdict': 'HOLD', 'run_date': '2026-11-10', 'last_price': 100.0, 'sector': 'X',
                                   'regime': 'BULL', 'trigger_type': 'MICRO', 'confidence': 50, 'quality': 70}]
    f.tables['call_outcomes'] = []
    return f


def test_load_calls_builds_every_source():
    with patch.object(co.db, 'supabase', _fake()):
        calls = co.load_calls('2026-11-01')
    by = {(c['source'], c['symbol']): c for c in calls}
    assert set(by) == {('advice', 'AAA'), ('target', 'BBB'), ('natwar', 'AAA'), ('natwar_v1', 'AAA'), ('universe', 'BBB')}
    assert by[('advice', 'AAA')]['direction'] == -1 and by[('advice', 'AAA')]['target'] == 'BBB'
    assert by[('natwar', 'AAA')]['attrs']['verdict'] == 'TRIM' and by[('natwar_v1', 'AAA')]['attrs']['verdict'] == 'SELL'
    assert by[('advice', 'AAA')]['attrs']['thesis_action'] == 'EXIT' and by[('advice', 'AAA')]['attrs']['weekday'] == 'Tue'


def test_update_is_idempotent_and_catches_up():
    f = _fake()
    with patch.object(co.db, 'supabase', f):
        n1 = co.update('2026-11-12')                      # bars through 11-12 -> td 1,2
        n2 = co.update('2026-11-12')                      # nothing new
        n3 = co.update('2026-11-14')                      # catch-up -> td 3,4
    rows = [r for r in f.tables['call_outcomes'] if r['source'] == 'advice']
    assert (n1, n2) == (5 * 2, 0) and n3 == 5 * 2 and [r['td'] for r in sorted(rows, key=lambda r: r['td'])] == [1, 2, 3, 4]
    assert rows[-1]['mae_pct'] <= rows[0]['mae_pct']      # running min carried across runs, not restarted
    assert [r['pair_pct'] for r in rows][0] == round(2 - (-1) - 0.3, 2)


def test_latest_summary_reads_stored_rows():
    f = _fake()
    with patch.object(co.db, 'supabase', f):
        co.update('2026-11-14')
        calls = co.load_calls('2026-11-01')
        s = co.latest_summary(calls)
    adv = s[('advice', 'primary', '2026-11-10', 'AAA')]
    assert adv['days'] == 4 and adv['correct'] is True     # AAA falls while Nifty rises: the sell is right


def test_default_through_skips_a_partial_bar():
    f = _fake()                                           # bars through 11-14, but 11-14 is still trading
    with patch.object(co.db, 'supabase', f), \
         patch.object(co.data_jobs, 'last_completed_trading_day', return_value='2026-11-13'):
        co.update()
    assert max(r['date'] for r in f.tables['call_outcomes']) == '2026-11-13'
