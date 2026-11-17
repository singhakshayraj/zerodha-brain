import importlib.util, os

_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'tax_replay.py')


def _load():
    s = importlib.util.spec_from_file_location('tax_replay', _p)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_summarise_nets_tax_against_drift():
    d = [dict(symbol='A', run_date='2026-09-01', action='sell after 2026-10-01', wait_days=20,
              tax_saved_inr=1000.0, drift_paid_inr=400.555, sell_value_inr=20000.0),
         dict(symbol='B', run_date='2026-09-01', action='sell after 2026-10-05', wait_days=22,
              tax_saved_inr=300.0, drift_paid_inr=100.0, sell_value_inr=10000.0),
         dict(symbol='C', run_date='2026-09-02', action='sell now', wait_days=None,
              tax_saved_inr=0, drift_paid_inr=0, sell_value_inr=5000.0)]
    assert _load().summarise(d) == {'calls': 3, 'told_to_wait': 2, 'tax_saved_inr': 1300.0,
                                    'drift_paid_inr': 500.56, 'net_inr': 799.44, 'net_pct': 2.66}
    assert _load().summarise([])['net_pct'] == 0.0
