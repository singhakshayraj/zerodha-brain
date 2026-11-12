"""DB-facing behaviour of this week's capture/reconciliation code, against an
in-memory Supabase (tests/fake_supabase.py). Each test pins one review fix
or capture guarantee that pure-function tests cannot see."""
import io
import json
from datetime import datetime
from unittest.mock import patch

import pytz

from tests.fake_supabase import FakeSupabase

IST = pytz.timezone('Asia/Kolkata')


# ── universe_calls ───────────────────────────────────────────────────────────

def _scan(verdict, price):
    return {'AAA': {'symbol': 'AAA', 'score': 60, 'sector': 'Information Technology',
                    'last_close': price,
                    'detail': {'verdict': verdict, 'trigger_type': 'MICRO', 'trend_score': 60,
                               'confidence': 70, 'last_price': price, 'indicators': {}}}}


def test_universe_log_first_scan_of_day_wins():
    import universe_calls as uc
    fake = FakeSupabase()
    with patch.object(uc.db, 'supabase', fake), \
         patch('fundamentals.load_store', return_value={'by_symbol': {}}):
        uc.log_scan(_scan('HOLD', 100.0), '2026-11-12', 'BULL')
        uc.log_scan(_scan('SELL', 90.0), '2026-11-12', 'BULL')       # forced re-run
    rows = fake.tables['universe_calls']
    assert len(rows) == 1
    assert (rows[0]['verdict'], rows[0]['last_price']) == ('HOLD', 100.0)


def test_universe_grade_due_writes_outcome_and_sector_variant():
    import universe_calls as uc
    days = [f'2026-11-{d:02d}' for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]
    candles = ([{'symbol': 'AAA', 'interval': 'day', 'trade_date': d, 'close': 100 + i} for i, d in enumerate(days)]
               + [{'symbol': 'NIFTY 50', 'interval': 'day', 'trade_date': d, 'close': 100} for d in days])
    fake = FakeSupabase({'candles': candles, 'universe_calls': [
        {'run_date': '2026-11-02', 'symbol': 'AAA', 'verdict': 'SELL', 'sector_verdict': 'TRIM',
         'trigger_type': 'MICRO', 'last_price': 100.0, 'evaluated_at': None}]})
    with patch.object(uc.db, 'supabase', fake):
        assert uc.grade_due('2026-11-14') == 1
    r = fake.tables['universe_calls'][0]
    assert r['outcome_return_pct'] == 9.0 and r['outcome_correct_alpha'] is False
    assert r['sector_outcome_correct_alpha'] is False and r['evaluated_at']


# ── reconcile (second account) ───────────────────────────────────────────────

def test_second_account_ledger_matches_the_shown_sector_call():
    import reconcile as rc
    days = ['2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06']
    fake = FakeSupabase({
        'tradebook': [{'account': 'natwar', 'symbol': 'AAA', 'trade_type': 'sell', 'quantity': 5,
                       'price': 100.0, 'trade_date': '2026-11-03', 'executed_at': '2026-11-03T14:00:00'}],
        'account_calls': [{'account': 'natwar', 'run_date': '2026-11-02', 'symbol': 'AAA',
                           'verdict': 'HOLD', 'sector_verdict': 'TRIM', 'quantity': 10,
                           'last_price': 100.0, 'rotation_target_symbol': None,
                           'created_at': '2026-11-06T04:30:00+00:00'}],
        'candles': ([{'symbol': 'NIFTY 50', 'interval': 'day', 'trade_date': d, 'close': 100} for d in days]
                    + [{'symbol': 'AAA', 'interval': 'day', 'trade_date': d, 'close': 95} for d in days]),
    })
    with patch.object(rc.db, 'supabase', fake):
        rc.run('natwar')
    led = fake.tables['decision_ledger']
    assert [r['kind'] for r in led] == ['followed_sell']          # v1 said HOLD; he was shown TRIM
    assert led[0]['call_verdict'] == 'TRIM' and led[0]['impact_inr'] == 25.0
    pnl = {r['date']: r for r in fake.tables['pnl_daily']}
    assert pnl['2026-11-03']['fills'] == 1


# ── scheduler: preview waits for the primary's official rows ─────────────────

def test_scan_landed_needs_official_primary_rows_not_just_universe():
    import scheduler as sch
    fake = FakeSupabase({'universe_calls': [{'run_date': '2026-11-12', 'symbol': 'X'}],
                         'portfolio_advice': [{'run_date': '2026-11-12', 'symbol': 'A', 'is_official': False}]})
    with patch.object(sch.db, 'supabase', fake):
        assert sch._scan_landed('2026-11-12') is False
        fake.tables['portfolio_advice'].append({'run_date': '2026-11-12', 'symbol': 'A', 'is_official': True})
        assert sch._scan_landed('2026-11-12') is True


# ── daily bars: held names (both accounts) ───────────────────────────────────

class _MD:
    def __init__(self, holdings, bars):
        self._instrument_cache = {}
        self.kite = type('K', (), {'get_holdings': lambda s: holdings})()
        self._bars = bars

    def get_candles(self, key, interval, days):
        return self._bars.get(key.split(':', 1)[1], [])


def _bars(dates):
    return [{'timestamp': f'{d}T00:00:00+0530', 'open': 1, 'high': 1, 'low': 1, 'close': 1, 'volume': 1}
            for d in dates]


def test_bar_sync_backfills_new_held_names_and_ignores_their_failures():
    import data_jobs as dj
    written, cfg = [], {'daily_bars_through': '2026-11-10'}
    bars = {'NIFTY 50': _bars(['2026-11-11', '2026-11-12']),
            'IDX1': _bars(['2026-11-11', '2026-11-12']),
            'ETF1': _bars(['2026-08-26', '2026-09-01', '2026-11-12'])}   # first seen: full window
    md = _MD([{'tradingsymbol': 'ETF1', 'instrument_token': 7},
              {'tradingsymbol': 'SGB1', 'instrument_token': 8}], bars)   # SGB1 has no bars at all
    fake = FakeSupabase({'candles': []})
    with patch.object(dj.config, 'NIFTY500_INSTRUMENT_TOKENS', {'NSE:IDX1': 1}), \
         patch.object(dj.db, 'supabase', fake), \
         patch.object(dj.db, 'get_config', side_effect=lambda k: cfg.get(k)), \
         patch.object(dj.db, 'write_config', side_effect=lambda k, v: cfg.__setitem__(k, v)), \
         patch.object(dj.db, 'upsert_candles', side_effect=lambda rows: written.extend(rows) or len(rows)), \
         patch.object(dj, 'preview_account_holdings', return_value=[('NWONLY', 9)]), \
         patch.object(dj, '_MAX_SYNC_FAILURES', 0):
        bars['NWONLY'] = _bars(['2026-09-01', '2026-11-12'])
        dj.sync_daily_bars(md, now_ist=IST.localize(datetime(2026, 11, 12, 18, 0)))
    by_sym = {}
    for r in written:
        by_sym.setdefault(r['symbol'], []).append(r['trade_date'])
    assert by_sym['ETF1'] == ['2026-08-26', '2026-09-01', '2026-11-12']   # backfilled from start
    assert by_sym['NWONLY'] == ['2026-09-01', '2026-11-12']               # second account's name
    assert by_sym['IDX1'] == ['2026-11-11', '2026-11-12']                 # watermark rule unchanged
    assert cfg['daily_bars_through'] == '2026-11-12'                      # SGB1 did not block it


# ── macro: front-month futures are monthly contracts ─────────────────────────

def test_front_month_tokens_pick_monthly_not_weekly():
    import macro_snapshot as ms
    dump = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
            "3404291,0,USDINR26N13FUT,USDINR,0,2026-11-13,0,0,1,FUT,CDS-FUT,CDS\n"
            "405507,0,USDINR26NOVFUT,USDINR,0,2026-11-26,0,0,1,FUT,CDS-FUT,CDS\n"
            "999,0,USDINR26DECFUT,USDINR,0,2026-12-29,0,0,1,FUT,CDS-FUT,CDS\n"
            "146796039,0,CRUDEOIL26NOVFUT,CRUDEOIL,0,2026-11-19,0,0,1,FUT,MCX-FUT,MCX\n")
    cfg = {}
    with patch.object(ms.urllib.request, 'urlopen', return_value=io.BytesIO(dump.encode())), \
         patch.object(ms.db, 'get_config', side_effect=lambda k: cfg.get(k)), \
         patch.object(ms.db, 'write_config', side_effect=lambda k, v: cfg.__setitem__(k, v)):
        toks = ms.front_month_tokens('2026-11-12')
    assert toks == {'usdinr': 405507, 'crude': 146796039}
    assert json.loads(cfg['macro_front_tokens'])['usdinr']['symbol'] == 'USDINR26NOVFUT'


# ── household: commits only for real (sized) buys ────────────────────────────

def test_preview_household_commit_blocks_second_pile_in_but_not_unsized():
    import account_preview as ap
    universe = [{'symbol': 'TGT', 'advisor_score': 90, 'sector': 'Information Technology',
                 'advisor_score_updated_at': '2026-11-12T10:00:00+05:30',
                 'advisor_detail': {'last_price': 100.0}}]
    rows = [{'symbol': 'S1', 'verdict': 'SELL', 'trend_score': -60, 'quantity': 25, 'last_price': 100.0, 'reasons': []},
            {'symbol': 'S2', 'verdict': 'SELL', 'trend_score': -60, 'quantity': 25, 'last_price': 100.0, 'reasons': []}]
    with patch.object(ap.db, 'get_universe_by_sector', return_value=universe, create=True), \
         patch.object(ap.market_regime, 'rotation_min_gap_for', return_value=40), \
         patch('household.other_accounts_positions', return_value={'BIG': 95_000}), \
         patch('household._index_weights', return_value={}):
        ap.attach_rotations(rows, '2026-11-12', 'trend')
    # each sale frees ~2.4% of the book; the first buy lands, the second would
    # take TGT past the 3% duplicate cap
    assert rows[0]['rotation_target_symbol'] == 'TGT'
    assert 'rotation_target_symbol' not in rows[1]
    assert 'already' in rows[1]['indicators']['household']

    rows2 = [dict(r, reasons=[]) for r in rows]
    unsized = [dict(universe[0], advisor_detail={'last_price': None})]   # no price -> no buy qty
    with patch.object(ap.db, 'get_universe_by_sector', return_value=unsized, create=True), \
         patch.object(ap.market_regime, 'rotation_min_gap_for', return_value=40), \
         patch('household.other_accounts_positions', return_value={'BIG': 95_000}), \
         patch('household._index_weights', return_value={}):
        ap.attach_rotations(rows2, '2026-11-12', 'trend')
    assert rows2[0]['rotation_target_symbol'] == 'TGT' and rows2[1]['rotation_target_symbol'] == 'TGT'
