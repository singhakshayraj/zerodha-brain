"""Real-money P&L: positions from an anchor + fills, MTM daily P&L, ledger."""
import reconcile as rc

DAYS = ['2026-11-02', '2026-11-03', '2026-11-04']
F = lambda d, t, sym, side, q, p: {'trade_date': d, 'executed_at': f'{d}T{t}', 'symbol': sym,
                                   'trade_type': side, 'quantity': q, 'price': p}


def test_positions_walk_back_and_forward_from_anchor():
    fills = [F('2026-11-02', '14:00:00', 'A', 'sell', 10, 100), F('2026-11-04', '14:00:00', 'B', 'buy', 5, 50)]
    pos = rc.positions_by_day({'A': 0, 'C': 7}, '2026-11-03T09:45:00', fills, DAYS)
    assert pos['2026-11-02'] == {'C': 7.0}                       # A sold that day: gone at EOD
    assert pos['2026-11-03'] == {'C': 7.0}
    assert pos['2026-11-04'] == {'C': 7.0, 'B': 5.0}
    pos_before = rc.positions_by_day({'C': 7}, '2026-11-03T09:45:00', fills, ['2026-11-01'])
    assert pos_before['2026-11-01'] == {'C': 7.0, 'A': 10.0}     # undo the 11-02 sale


def test_daily_pnl_mtm_handles_trades_and_round_trips():
    closes = {'A': {'2026-11-02': 100, '2026-11-03': 110, '2026-11-04': 99},
              'B': {'2026-11-03': 50, '2026-11-04': 60},
              'NIFTY 50': {'2026-11-02': 100, '2026-11-03': 101, '2026-11-04': 101}}
    pos = {'2026-11-02': {'A': 10}, '2026-11-03': {'A': 10}, '2026-11-04': {'B': 5}}
    fills = [F('2026-11-04', '10:00:00', 'A', 'sell', 10, 105), F('2026-11-04', '11:00:00', 'B', 'buy', 5, 55),
             F('2026-11-04', '12:00:00', 'B', 'buy', 2, 56), F('2026-11-04', '13:00:00', 'B', 'sell', 2, 58)]
    r = {x['date']: x for x in rc.daily_pnl(DAYS, pos, closes, fills)}
    assert r['2026-11-03']['pnl'] == 100.0                       # 10 × (110−100)
    assert r['2026-11-03']['nifty_pnl'] == 10.0                  # 1000 × 1%
    # 11-04: A sold at 105 vs 110 prev close = −50; B 5 sh bought 55 → 60 = +25; B round trip +4
    assert r['2026-11-04']['pnl'] == -21.0
    assert r['2026-11-04']['fills'] == 4


def test_ledger_tags_followed_own_and_ignored():
    days = ['2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09']
    closes = {'A': {d: 90 for d in days}, 'T': {d: 120 for d in days}, 'X': {d: 10 for d in days},
              'H': {d: 80 for d in days}}
    calls = [{'symbol': 'A', 'run_date': '2026-11-02', 'verdict': 'SELL', 'quantity': 10, 'last_price': 100,
              'rotation_target_symbol': 'T'},
             {'symbol': 'H', 'run_date': '2026-11-02', 'verdict': 'TRIM', 'quantity': 10, 'last_price': 100,
              'rotation_target_symbol': None}]
    fills = [F('2026-11-03', '14:00:00', 'A', 'sell', 10, 100), F('2026-11-03', '14:05:00', 'T', 'buy', 1, 100),
             F('2026-11-03', '15:00:00', 'X', 'buy', 3, 12)]
    led = {(r['kind'], r['symbol']): r for r in rc.ledger(fills, calls, closes, days)}
    assert led[('followed_sell', 'A')]['impact_inr'] == 100.0     # sold 100, later 90
    assert led[('followed_buy', 'T')]['rotation_from'] == 'A' and led[('followed_buy', 'T')]['impact_inr'] == 20.0
    assert led[('own_buy', 'X')]['impact_inr'] == -6.0
    ign = led[('ignored_call', 'H')]
    assert ign['qty'] == 5 and ign['impact_inr'] == -100.0        # held 5 more from 100 → 80


def test_ledger_merges_partial_fills_at_vwap():
    days = ['2026-11-02', '2026-11-03', '2026-11-04']
    closes = {'A': {d: 90 for d in days}}
    fills = [F('2026-11-03', '14:00:00', 'A', 'sell', 4, 100), F('2026-11-03', '14:00:01', 'A', 'sell', 6, 105)]
    led = rc.ledger(fills, [], closes, days)
    assert len(led) == 1 and led[0]['qty'] == 10 and led[0]['price'] == 103.0
    assert led[0]['kind'] == 'own_sell' and led[0]['impact_inr'] == 130.0


def test_unsettled_buys_are_added_on_top_of_a_post_trade_snapshot():
    fills = [F('2026-11-04', '14:00:00', 'B', 'buy', 5, 50), F('2026-11-04', '14:10:00', 'C', 'sell', 3, 10)]
    # snapshot at 18:00 already reflects the sell (C 7 -> 4) but not the T+1 buy
    pos = rc.positions_by_day({'C': 4}, '2026-11-04T18:00:00', fills, DAYS, unsettled_from='2026-11-03')
    assert pos['2026-11-04'] == {'C': 4.0, 'B': 5.0}
    assert pos['2026-11-03'] == {'C': 7.0}


def test_ist_parses_short_microseconds():
    assert rc._ist('2026-11-11T13:33:18.41192+00:00') == '2026-11-11T19:03:18'
