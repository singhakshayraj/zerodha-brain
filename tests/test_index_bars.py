"""index_bars — daily closes of every NSE index from the public archive [P-68 phase 4]."""
from unittest.mock import patch
import index_bars as ib
from tests.fake_supabase import FakeSupabase

CSV = ("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,"
       "Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
       "Nifty 50,13-11-2026,25209.85,25246.35,25054.4,25144.3,-155.05,-.61,239573267,25397.2,20.49,3.08,1.4\n"
       "Nifty 500,13-11-2026,23794.55,23858.4,23649.2,23734.2,-120.1,-.5,1,2,21.1,3.2,1.3\n"
       "Nifty PSU Bank,13-11-2026,9293.5,9335.8,9210.55,9258.6,-10,-.1,-,-,-,-,-\n")


def test_parse_reads_nifty_500_row():
    rows = ib.parse(CSV)
    r = next(r for r in rows if r['index_name'] == 'Nifty 500')
    assert r == {'index_name': 'Nifty 500', 'trade_date': '2026-11-13', 'open': 23794.55, 'high': 23858.4,
                 'low': 23649.2, 'close': 23734.2, 'pe': 21.1, 'pb': 3.2, 'div_yield': 1.3}
    assert next(r for r in rows if r['index_name'] == 'Nifty PSU Bank')['pe'] is None


def test_sync_skips_404_days_and_advances_the_watermark():
    f = FakeSupabase()
    f.tables['index_bars'] = []
    f.tables['app_config'] = [{'key': ib.KEY, 'value': '2026-11-12'}]
    served = {'2026-11-13': CSV}                                  # 11-14/15 weekend, 11-16 not published yet
    with patch.object(ib.db, 'supabase', f), patch.object(ib, 'fetch', lambda d: served.get(d)):
        n = ib.sync(through='2026-11-16')
    assert n == 3
    assert {r['trade_date'] for r in f.tables['index_bars']} == {'2026-11-13'}
    assert next(r['value'] for r in f.tables['app_config'] if r['key'] == ib.KEY) == '2026-11-13'


def test_url_for_a_day():
    assert ib.url('2026-11-13') == 'https://nsearchives.nseindia.com/content/indices/ind_close_all_13112026.csv'
