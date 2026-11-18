import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import lab_backtest as bt  # noqa: E402


def test_histkite_serves_only_closed_bars():
    starts = ['2024-01-02 09:15', '2024-01-02 09:20', '2024-01-02 09:25']
    k = bt.HistKite({'X': (starts, [{'i': i} for i in range(3)])})
    got = k.get_historical_data('X', '5minute', '2024-01-02 00:00:00', '2024-01-02 09:29:59')
    assert got == [{'i': 0}, {'i': 1}]          # 09:25 bar closes at 09:30: not yet


def test_universe_is_point_in_time_and_drops_gapped_symbol():
    days = pd.bdate_range('2024-01-01', periods=30).strftime('%Y-%m-%d')
    rows = []
    for i, d in enumerate(days):
        rows.append({'date': d, 'symbol': 'A', 'open': 100, 'close': 100, 'volume': 1000})
        b = 100 if i < 25 else 40                      # day 25: 60% overnight drop (split)
        rows.append({'date': d, 'symbol': 'B', 'open': b, 'close': b, 'volume': 2000})
    u = bt.universes(pd.DataFrame(rows), n=2)
    assert days[19] not in u                           # first 20 sessions: no ADV yet
    assert [s for s, _, _ in u[days[20]]] == ['B', 'A']
    assert [s for s, _, _ in u[days[25]]] == ['A']     # B out on the gap day and after


def test_to_5min_dedupes_and_anchors_at_0915():
    ts = pd.to_datetime(['2024-01-02 09:15', '2024-01-02 09:16', '2024-01-02 09:16',
                         '2024-01-02 09:20'])
    df = pd.DataFrame({'Date': ts, 'Open': [1, 2, 2, 3], 'High': [1, 5, 5, 3],
                       'Low': [1, 2, 2, 3], 'Close': [1, 2, 2, 3], 'Volume': [10, 20, 20, 30]})
    out = bt.to_5min(df)
    assert out['Date'].dt.strftime('%H:%M').tolist() == ['09:15', '09:20']
    assert out['Volume'].tolist() == [30, 30] and out['High'].tolist() == [5, 3]


def test_verdict_needs_every_block_positive():
    def tr(d, net):
        return {'strategy_id': 'CONF_n1_base_v1', 'trade_date': d, 'net_inr': net, 'gross_inr': net,
                'charges_inr': 0, 'slippage_inr': 0, 'gross_r': net / 100, 'cost_r': 0,
                'net_r': net / 100}
    days = pd.bdate_range('2018-03-01', '2025-12-31').strftime('%Y-%m-%d')
    good = [x for i, d in enumerate(days) for x in (tr(d, 300 + (i % 3) * 10), tr(d, -50))]  # PF ~6
    v = {r['id']: r for r in bt.verdicts(good, len(days))}['CONF_n1_base_v1']
    assert v['pass'] and v['sprt'] == 'promoted'
    bad = [t if t['trade_date'] < '2023-01-01' else dict(t, net_inr=-1, net_r=-0.01) for t in good]
    v = {r['id']: r for r in bt.verdicts(bad, len(days))}['CONF_n1_base_v1']
    assert not v['pass'] and v['blocks'][2] < 0
