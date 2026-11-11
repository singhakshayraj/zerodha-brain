import os, sys, tempfile
sys.path.insert(0, 'scripts')
import import_fii_dii as fd


def test_parse_and_merge_keep_other_keys():
    p = os.path.join(tempfile.mkdtemp(), 'h.csv')
    open(p, 'w').write("date,fii_buy,fii_sell,fii_net,dii_buy,dii_sell,dii_net\n"
                       "2026-11-11,30087.21,17523.88,12563.33,19704.39,28138.92,-8434.53\nbad,row\n")
    f = fd.parse(p)
    assert list(f) == ['2026-11-11'] and f['2026-11-11']['fii_net_cr'] == 12563.33
    m = fd.merge({'nifty': {'close': 25255}, 'fii_net_cr': None}, f['2026-11-11'])
    assert m['nifty']['close'] == 25255 and m['fii']['net'] == 12563.33 and m['dii_net_cr'] == -8434.53
