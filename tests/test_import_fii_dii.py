import os, sys, tempfile
sys.path.insert(0, 'scripts')
import import_fii_dii as fd


def _csv(name, text):
    d = tempfile.mkdtemp(); p = os.path.join(d, name); open(p, 'w').write(text); return p


def test_cash_and_merge_keep_other_keys():
    rows = fd._read(_csv('h.csv', "date,fii_buy,fii_sell,fii_net,dii_buy,dii_sell,dii_net\n"
                                  "2026-11-11,30087.21,17523.88,12563.33,19704.39,28138.92,-8434.53\nbad,row\n"))
    c = fd.parse_cash(rows)
    assert list(c) == ['2026-11-11'] and c['2026-11-11']['fii_net_cr'] == 12563.33
    m = fd.merge({'nifty': {'close': 25255}, 'fii_net_cr': None}, c['2026-11-11'], {'fut': {'fii_net': -70413.0}})
    assert m['nifty']['close'] == 25255 and m['fii']['net'] == 12563.33 and m['fut']['fii_net'] == -70413.0


def test_futures_flags_exact_repeat_of_previous_session():
    rows = fd._read(_csv('f.csv', "date,fii_long,fii_short,fii_long_share_pct,fii_net,dii_net,pro_net,client_net\n"
                                  "2026-11-08,49050,150509,24.6,-101459,35536,-15846,81769\n"
                                  "2026-11-06,48656,149072,24.6,-100416,35536,-18901,83781\n"
                                  "2026-11-09,49050,150509,24.6,-101459,35536,-15846,81769\n"))
    f = fd.parse_futures(rows)
    assert f['2026-11-06']['fut']['dup_of_prev'] is False
    assert f['2026-11-08']['fut']['dup_of_prev'] is False       # DII repeats but the others differ
    assert f['2026-11-09']['fut']['dup_of_prev'] is True        # all six identical to 11-08
