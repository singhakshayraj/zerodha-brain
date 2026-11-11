import importlib.util
import os

_p = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'scripts', 'grade_buy_calls.py')
_spec = importlib.util.spec_from_file_location('grade_buy_calls', _p)
gb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gb)


def _series(start, vals):
    return [(f'2026-01-{start + i:02d}', v) for i, v in enumerate(vals)]


def test_grade_alpha_and_maturity():
    index = _series(1, [100, 100, 101, 102])          # +2% over 3 bars
    closes = {'AAA': _series(1, [50, 51, 52, 55]),     # +10% -> hit
              'BBB': _series(1, [50, 50, 50, 50])}     # 0% -> miss
    calls = [{'run_date': '2026-01-01', 'target': 'AAA'},
             {'run_date': '2026-01-01', 'target': 'BBB'},
             {'run_date': '2026-01-02', 'target': 'AAA'}]   # not matured at h=3
    g = gb.grade(calls, closes, index, 3)
    assert [(x['target'], x['hit']) for x in g] == [('AAA', 1), ('BBB', 0)]
    assert round(g[0]['alpha'], 4) == 0.08


def test_entry_is_first_bar_on_or_after_run_date():
    index = _series(1, [100, 100, 100, 100])
    closes = {'AAA': _series(2, [10, 11, 12])}         # no bar on 01-01
    g = gb.grade([{'run_date': '2026-01-01', 'target': 'AAA'}], closes, index, 2)
    assert g[0]['entry_date'] == '2026-01-02' and g[0]['exit_date'] == '2026-01-04'
