"""Universe calls: scan verdicts are kept and graded by the holdings rules."""
import universe_calls as uc


def _scored():
    return {
        'AAA': {'symbol': 'AAA', 'score': 60, 'sector': 'IT', 'last_close': 100.0, 'weekly': 'up',
                'detail': {'verdict': 'HOLD', 'trigger_type': 'MICRO', 'trend_score': 60,
                           'confidence': 70, 'last_price': 100.0,
                           'indicators': {'rsi_14': 55, 'junk': 1}}},
        'BBB': {'symbol': 'BBB', 'score': -40, 'sector': 'IT', 'last_close': 50.0,
                'detail': {'verdict': 'SELL', 'trigger_type': 'MACRO', 'trend_score': -40,
                           'confidence': 65, 'last_price': 50.0, 'indicators': {}}},
        'NOD': {'symbol': 'NOD', 'score': 5, 'sector': 'IT', 'detail': None},
    }


def test_rows_from_scan_keeps_verdicts_and_drops_no_detail():
    rows = uc.rows_from_scan(_scored(), '2026-11-11', 'AGGRESSIVE_BULL', {'AAA': 80})
    assert {r['symbol'] for r in rows} == {'AAA', 'BBB'}
    a = next(r for r in rows if r['symbol'] == 'AAA')
    assert (a['verdict'], a['quality'], a['regime'], a['last_price']) == ('HOLD', 80, 'AGGRESSIVE_BULL', 100.0)
    assert a['indicators'] == {'rsi_14': 55}                      # whitelist only


def _bars(start, closes):
    return [{'timestamp': f'2026-11-{start + i:02d}', 'close': c} for i, c in enumerate(closes)]


def test_grade_rows_uses_horizon_and_market_neutral_label():
    queue = [{'run_date': '2026-11-02', 'symbol': 'AAA', 'verdict': 'HOLD', 'trigger_type': 'MICRO', 'last_price': 100.0},
             {'run_date': '2026-11-02', 'symbol': 'BBB', 'verdict': 'SELL', 'trigger_type': 'MACRO', 'last_price': 50.0}]
    bars = {'AAA': _bars(2, [100] * 9 + [105]),                   # 10 bars: +5%
            'BBB': _bars(2, [50] * 10),                           # MACRO needs 30: not due
            'NIFTY 50': _bars(2, [100] * 9 + [102])}              # +2%
    done = uc.grade_rows(queue, bars)
    assert [d['symbol'] for d in done] == ['AAA']
    d = done[0]
    assert d['evaluation_horizon_days'] == 10
    assert d['outcome_return_pct'] == 5.0 and d['outcome_vs_nifty_pct'] == 3.0
    assert d['outcome_correct_alpha'] is True and d['evaluated_at']
