import call_scoring as cs

NIFTY = {f'2026-11-{d:02d}': 100 + i for i, d in enumerate((3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 17, 18))}
DAYS = sorted(NIFTY)


def _bars(sym_closes):
    return {'NIFTY 50': NIFTY, **sym_closes}


def test_sell_call_wrong_three_days_then_right():
    # entry 100 on 11-03; stock rises to 104 then falls to 90; Nifty +1/day
    closes = dict(zip(DAYS[1:], [102, 103, 104, 96, 93, 90, 90, 90, 90, 90, 90]))
    call = {'source': 'advice', 'account': 'primary', 'run_date': '2026-11-03', 'symbol': 'AAA',
            'entry': 100.0, 'direction': -1, 'target': None, 'target_entry': None}
    rows = cs.score_call(call, _bars({'AAA': closes}), DAYS)
    assert [r['td'] for r in rows] == list(range(1, 12))
    assert rows[0]['ret_pct'] == 2.0 and rows[0]['nifty_pct'] == 1.0 and rows[0]['signed_alpha_pct'] == -1.0
    assert rows[3]['signed_alpha_pct'] == 8.0            # 96: ret -4, nifty +4 -> alpha -8, signed +8
    assert rows[3]['mae_pct'] == -1.0 and rows[3]['mfe_pct'] == 8.0
    s = cs.summarize(rows, -1)
    assert s['correct'] and s['early_wrong_days'] == 3 and s['at'][5] == rows[4]['signed_alpha_pct'] and s['at'][20] is None


def test_pair_after_costs_and_pair_without_target_bars():
    closes = dict(zip(DAYS[1:], [100] * 11)); tgt = dict(zip(DAYS[1:], [110] * 11))
    call = {'source': 'advice', 'account': 'primary', 'run_date': '2026-11-03', 'symbol': 'AAA',
            'entry': 100.0, 'direction': -1, 'target': 'BBB', 'target_entry': 100.0}
    rows = cs.score_call(call, _bars({'AAA': closes, 'BBB': tgt}), DAYS)
    assert rows[0]['pair_pct'] == 9.7                      # +10 target, 0 sold, -0.3 cost
    rows = cs.score_call(call, _bars({'AAA': closes}), DAYS)
    assert rows[0]['pair_pct'] is None and rows[0]['ret_pct'] == 0.0


def test_missing_symbol_bar_is_a_gap():
    closes = {d: 100 for d in DAYS[1:]}; del closes[DAYS[3]]
    call = {'source': 'universe', 'account': 'universe', 'run_date': '2026-11-03', 'symbol': 'AAA',
            'entry': 100.0, 'direction': 1, 'target': None, 'target_entry': None}
    rows = cs.score_call(call, _bars({'AAA': closes}), DAYS)
    assert [r['td'] for r in rows][:4] == [1, 2, 4, 5] and len(rows) == 10


def test_hold_should_have_sold_and_horizon_cap():
    days = [f'2027-01-{d:02d}' for d in range(1, 32)] + [f'2027-02-{d:02d}' for d in range(1, 29)] + [f'2027-03-{d:02d}' for d in range(1, 15)]
    nifty = {d: 100 for d in days}; closes = {d: 88 for d in days}
    call = {'source': 'advice', 'account': 'primary', 'run_date': days[0], 'symbol': 'AAA',
            'entry': 100.0, 'direction': 1, 'target': None, 'target_entry': None}
    rows = cs.score_call(call, {'NIFTY 50': nifty, 'AAA': closes}, days)
    assert len(rows) == 60 and rows[-1]['td'] == 60
    assert cs.summarize(rows, 1)['should_have_sold'] is True and cs.direction('INSUFFICIENT') == 0


def test_sector_alpha_signed_by_direction():
    from call_scoring import score_call
    days = ['2026-11-10', '2026-11-11']
    bars = {'NIFTY 50': {d: 100 for d in days}, 'AAA': {'2026-11-10': 100, '2026-11-11': 99}}
    for i, s in enumerate(['C1', 'C2', 'C3', 'C4', 'C5']):
        bars[s] = {'2026-11-10': 100, '2026-11-11': 102}           # cohort +2%
    call = {'source': 'advice', 'account': 'primary', 'run_date': '2026-11-10', 'symbol': 'AAA',
            'entry': 100.0, 'direction': -1, 'target': None, 'target_entry': None}
    rows = score_call(call, bars, days, cohorts={'sector': ['C1', 'C2', 'C3', 'C4', 'C5'], 'size': ['C1']})
    assert rows[0]['sector_alpha_pct'] == 3.0       # SELL: -(−1 − 2)
    assert rows[0]['size_alpha_pct'] is None         # cohort too thin
    plain = score_call(call, bars, days)
    assert plain[0]['sector_alpha_pct'] is None and plain[0]['size_alpha_pct'] is None


def test_summarize_carries_cohort_alpha_at_checkpoints():
    rows = [{'td': t, 'signed_alpha_pct': 1.0, 'mae_pct': 0.0, 'mfe_pct': 1.0,
             'sector_alpha_pct': 2.0 if t == 5 else None, 'size_alpha_pct': None} for t in (1, 5)]
    s = cs.summarize(rows, 1)
    assert s['at_sector'][5] == 2.0 and s['at_sector'][10] is None and s['at_size'][5] is None
