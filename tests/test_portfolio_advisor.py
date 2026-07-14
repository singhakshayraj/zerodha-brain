"""Portfolio Advisor — verdict logic for real long-term holdings. ADVISORY
ONLY: these tests also pin that the module never touches an order path."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import portfolio_advisor as pa


def _candles(n, start=100.0, step=0.5):
    """n daily bars trending by `step`/bar (negative = downtrend)."""
    out = []
    p = start
    for i in range(n):
        p += step
        out.append({'open': p - 0.2, 'high': p + 1.0, 'low': p - 1.0,
                    'close': p, 'volume': 1000,
                    'timestamp': f'2026-01-{(i % 28) + 1:02d}'})
    return out


def _holding(avg=100.0, last=100.0, qty=10):
    return {'symbol': 'X', 'quantity': qty,
            'average_price': avg, 'last_price': last}


# --- verdicts ---

def test_uptrend_is_hold_with_stop_line():
    candles = _candles(250, start=100, step=0.5)   # steady climb
    out = pa.advise(_holding(avg=150, last=candles[-1]['close']), candles)
    assert out['verdict'] == 'HOLD'
    assert out['trend_score'] >= 20
    assert out['stop_level'] is not None            # hold-while-above line
    assert any('above' in r.lower() for r in out['reasons'])


def test_confirmed_downtrend_is_sell():
    candles = _candles(250, start=400, step=-1.0)   # steady bleed
    last = candles[-1]['close']
    out = pa.advise(_holding(avg=400, last=last), candles)
    assert out['verdict'] in ('SELL', 'SELL_ON_BOUNCE')
    assert out['trend_score'] <= -20


def test_insufficient_history():
    out = pa.advise(_holding(), _candles(10))
    assert out['verdict'] == 'INSUFFICIENT'
    assert out['confidence'] == 0


def test_breakeven_math_is_honest():
    # 40% down needs +66.7% back
    assert pa.breakeven_gain_pct(100.0, 60.0) == 66.7
    assert pa.breakeven_gain_pct(100.0, 100.0) == 0.0
    assert pa.breakeven_gain_pct(100.0, 120.0) == 0.0   # in profit → 0


def test_deep_loss_reason_included_when_losing():
    candles = _candles(250, start=400, step=-1.0)
    last = candles[-1]['close']
    out = pa.advise(_holding(avg=last * 2, last=last), candles)
    assert any('break even' in r for r in out['reasons'])


def test_swing_levels():
    candles = _candles(30, start=100, step=0)
    support, resistance = pa.swing_levels(candles)
    assert support is not None and resistance is not None
    assert support < resistance


# --- runner: advisory-only + resilience ---

def _md(holdings, candles):
    md = MagicMock()
    md.kite.get_holdings.return_value = holdings
    md.get_candles.return_value = candles
    return md


def test_run_advisor_stores_rows_and_places_nothing():
    md = _md([{'tradingsymbol': 'INFY', 'exchange': 'NSE', 'quantity': 5,
               'average_price': 1500.0, 'last_price': 1074.0}],
             _candles(250, start=1500, step=-1.5))
    with patch.object(pa.db, 'write_official_portfolio_advice',
                      side_effect=lambda rows: len(rows)) as up:
        n = pa.run_advisor(md)
    assert n == 1
    row = up.call_args.args[0][0]
    assert row['symbol'] == 'INFY'
    assert row['verdict'] in ('SELL', 'SELL_ON_BOUNCE', 'TRIM', 'HOLD')
    assert 'run_date' in row
    # advisory only — no order-path method was ever touched
    for name in ('place_buy_order', 'place_sell_order', 'place_order'):
        assert not getattr(md.kite, name).called


def test_run_advisor_skips_zero_qty_and_survives_symbol_failure():
    md = MagicMock()
    md.kite.get_holdings.return_value = [
        {'tradingsymbol': 'SOLD', 'quantity': 0},
        {'tradingsymbol': 'BROKEN', 'quantity': 5, 'average_price': 10,
         'last_price': 9},
        {'tradingsymbol': 'GOOD', 'quantity': 5, 'average_price': 10,
         'last_price': 12},
    ]
    good = _candles(250, start=10, step=0.05)

    def candles_for(key, interval, days):
        if 'BROKEN' in key:
            raise RuntimeError('boom')
        return good

    md.get_candles.side_effect = candles_for
    with patch.object(pa.db, 'write_official_portfolio_advice',
                      side_effect=lambda rows: len(rows)):
        n = pa.run_advisor(md)
    assert n == 1   # only GOOD; SOLD skipped, BROKEN failed but didn't abort


def test_run_advisor_no_holdings():
    md = MagicMock()
    md.kite.get_holdings.return_value = []
    assert pa.run_advisor(md) == 0


# --- real tradebook: stats, reason folding, daily sync ---

def _fill(sym, side, qty, price, date='2026-05-01'):
    return {'symbol': sym, 'trade_type': side, 'quantity': qty,
            'price': price, 'trade_date': date}


def test_tradebook_stats_realized_pnl():
    # buy 10 @ 100, sell 5 @ 120 → realized +100 vs avg cost
    stats = pa.tradebook_stats([
        _fill('X', 'buy', 10, 100.0),
        _fill('X', 'sell', 5, 120.0, '2026-05-02'),
    ])
    assert stats['X']['trades'] == 2
    assert stats['X']['realized_pnl'] == 100.0
    assert stats['X']['last_trade_date'] == '2026-05-02'


def test_tradebook_stats_survives_garbage():
    stats = pa.tradebook_stats([{'symbol': 'X', 'quantity': 'bad', 'price': 1},
                                _fill('Y', 'buy', 1, 10.0)])
    assert 'Y' in stats


def test_history_folded_into_reasons():
    candles = _candles(250, start=400, step=-1.0)
    last = candles[-1]['close']
    out = pa.advise(_holding(avg=last * 2, last=last), candles,
                    history={'trades': 14, 'realized_pnl': -2300.0,
                             'buy_qty': 10, 'buy_value': 100, 'sell_qty': 5,
                             'last_trade_date': '2026-05-25'})
    joined = ' '.join(out['reasons'])
    assert '14 fills' in joined
    assert 'realized and unrealized' in joined      # losing both ways flagged
    assert out['indicators']['history']['trades'] == 14


def test_sync_tradebook_normalizes_and_is_read_only():
    kite = MagicMock()
    kite.get_account_trades.return_value = [{
        'tradingsymbol': 'INFY', 'exchange': 'NSE', 'transaction_type': 'BUY',
        'quantity': 5, 'average_price': 1500.5, 'trade_id': 't1',
        'order_id': 'o1', 'fill_timestamp': '2026-07-13 10:01:00',
    }]
    with patch.object(pa.db, 'upsert_tradebook',
                      side_effect=lambda rows: len(rows)) as up:
        assert pa.sync_tradebook(kite) == 1
    row = up.call_args.args[0][0]
    assert row['trade_type'] == 'buy'
    assert row['trade_date'] == '2026-07-13'
    assert row['source'] == 'kite_daily'
    for name in ('place_buy_order', 'place_sell_order', 'place_order'):
        assert not getattr(kite, name).called


def test_sync_tradebook_survives_api_error():
    kite = MagicMock()
    kite.get_account_trades.side_effect = RuntimeError('down')
    assert pa.sync_tradebook(kite) == 0


# --- v2: trend consistency, relative strength, overextension, volume, concentration ---

def test_trend_consistency_all_above():
    # steady climb -> price consistently above its own 50-day EMA
    closes = [float(c['close']) for c in _candles(120, start=100, step=0.5)]
    c = pa.trend_consistency(closes)
    assert c is not None and c >= 80


def test_trend_consistency_none_with_insufficient_data():
    assert pa.trend_consistency([100.0] * 10) is None


def test_relative_strength_outperformance():
    stock = [float(c['close']) for c in _candles(30, start=100, step=1.0)]
    nifty = [float(c['close']) for c in _candles(30, start=100, step=0.2)]
    rs = pa.relative_strength(stock, nifty)
    assert rs is not None and rs > 0   # stock ran faster than the benchmark


def test_relative_strength_none_without_benchmark():
    stock = [float(c['close']) for c in _candles(30, start=100, step=1.0)]
    assert pa.relative_strength(stock, []) is None


def test_volume_trend_building():
    candles = []
    for i in range(20):
        vol = 1000 if i < 10 else 3000
        candles.append({'close': 100, 'open': 100, 'high': 101, 'low': 99, 'volume': vol})
    vt = pa.volume_trend(candles)
    assert vt is not None and vt > 1.3


def test_volume_trend_none_with_insufficient_data():
    assert pa.volume_trend([{'volume': 100}] * 5) is None


def test_overextension_downgrades_hold_to_trim():
    # a sharp, low-volatility spike far above EMA50 with RSI pinned high
    base = _candles(230, start=100, step=0.3)   # gentle base uptrend
    spike = _candles(20, start=base[-1]['close'], step=8.0)  # violent spike
    candles = base + spike
    out = pa.advise(_holding(avg=100, last=candles[-1]['close']), candles)
    assert out['indicators']['overextended'] is True
    assert out['verdict'] == 'TRIM'
    assert any('extended' in r.lower() for r in out['reasons'])


def test_concentration_flag_appears_above_threshold():
    candles = _candles(250, start=100, step=0.5)
    out = pa.advise(_holding(avg=150, last=candles[-1]['close']), candles,
                    portfolio_weight_pct=40.0)
    assert any('Concentration' in r for r in out['reasons'])
    assert out['indicators']['portfolio_weight_pct'] == 40.0


def test_concentration_flag_absent_below_threshold():
    candles = _candles(250, start=100, step=0.5)
    out = pa.advise(_holding(avg=150, last=candles[-1]['close']), candles,
                    portfolio_weight_pct=5.0)
    assert not any('Concentration' in r for r in out['reasons'])


def test_relative_strength_folded_into_score_and_reasons():
    stock = [float(c['close']) for c in _candles(230, start=100, step=0.5)]
    strong_bench = [s * 0.5 for s in stock]      # benchmark far weaker
    weak_bench = [100.0] * 230                    # benchmark flat
    candles = _candles(230, start=100, step=0.5)
    out_neutral = pa.advise(_holding(avg=100, last=candles[-1]['close']), candles)
    out_with_rs = pa.advise(_holding(avg=100, last=candles[-1]['close']), candles,
                            nifty_closes=weak_bench)
    assert out_with_rs['indicators']['relative_strength_vs_nifty'] is not None
    assert out_with_rs['trend_score'] >= out_neutral['trend_score']


# --- news sentiment factor ---

def test_news_sentiment_averages_recent_scores():
    with patch.object(pa.db, 'recent_news_for_symbol', return_value=[
        {'sentiment_score': 0.6}, {'sentiment_score': 0.2},
        {'sentiment_score': None},
    ]):
        assert pa.news_sentiment('INFY') == 0.4


def test_news_sentiment_none_without_coverage():
    with patch.object(pa.db, 'recent_news_for_symbol', return_value=[]):
        assert pa.news_sentiment('INFY') is None


def test_news_sentiment_shifts_score_and_reasons():
    candles = _candles(250, start=100, step=0.5)
    h = _holding(avg=100, last=candles[-1]['close'])
    neutral = pa.advise(h, candles)
    negative = pa.advise(h, candles, news_sent=-0.5)
    assert negative['trend_score'] < neutral['trend_score']
    assert any('news sentiment negative' in r.lower() for r in negative['reasons'])
    assert negative['indicators']['news_sentiment'] == -0.5


def test_news_sentiment_none_contributes_zero():
    candles = _candles(250, start=100, step=0.5)
    h = _holding(avg=100, last=candles[-1]['close'])
    assert (pa.advise(h, candles)['trend_score']
            == pa.advise(h, candles, news_sent=None)['trend_score'])


# --- runner: seeds instrument tokens (the 0-daily-bars bug fix) ---

def test_run_advisor_seeds_instrument_token_cache():
    md = MagicMock()
    md._instrument_cache = {}
    md.kite.get_holdings.return_value = [
        {'tradingsymbol': 'INFY', 'exchange': 'NSE', 'quantity': 5,
         'average_price': 1500.0, 'last_price': 1074.0,
         'instrument_token': 408065},
    ]
    md.get_candles.return_value = _candles(250, start=1500, step=-1.5)
    with patch.object(pa.db, 'write_official_portfolio_advice', side_effect=lambda r: len(r)):
        pa.run_advisor(md)
    assert md._instrument_cache.get('NSE:INFY') == 408065


# --- run_advisor_lite (2026-07-14 intraday refresh) ------------------------

def test_run_advisor_lite_stores_snapshot_rows_not_official():
    md = _md([{'tradingsymbol': 'INFY', 'exchange': 'NSE', 'quantity': 5,
               'average_price': 1500.0, 'last_price': 1074.0}],
             _candles(250, start=1500, step=-1.5))
    with patch.object(pa.db, 'insert_portfolio_advice_snapshot',
                      side_effect=lambda rows: len(rows)) as ins, \
         patch.object(pa.db, 'get_official_advice_for_date', return_value=[]), \
         patch.object(pa.db, 'get_tradebook', return_value=[]):
        n = pa.run_advisor_lite(md)
    assert n == 1
    row = ins.call_args.args[0][0]
    assert row['is_official'] is False
    assert 'run_id' in row
    # advisory only — no order-path method was ever touched
    for name in ('place_buy_order', 'place_sell_order', 'place_order'):
        assert not getattr(md.kite, name).called


def test_run_advisor_lite_never_rescans_rotation_or_sends_digest():
    md = _md([{'tradingsymbol': 'INFY', 'exchange': 'NSE', 'quantity': 5,
               'average_price': 1500.0, 'last_price': 1074.0}],
             _candles(250, start=1500, step=-1.5))
    with patch.object(pa.db, 'insert_portfolio_advice_snapshot',
                      side_effect=lambda rows: len(rows)), \
         patch.object(pa.db, 'get_official_advice_for_date', return_value=[]), \
         patch.object(pa.db, 'get_tradebook', return_value=[]), \
         patch.object(pa, 'score_universe') as scan, \
         patch.object(pa, 'send_daily_digest') as digest:
        pa.run_advisor_lite(md)
    scan.assert_not_called()
    digest.assert_not_called()


def test_run_advisor_lite_carries_forward_rotation_from_official_row():
    md = _md([{'tradingsymbol': 'NTPC', 'exchange': 'NSE', 'quantity': 5,
               'average_price': 100.0, 'last_price': 90.0}],
             _candles(250, start=100, step=-1.0))
    official_row = {
        'symbol': 'NTPC', 'rotation_target_symbol': 'PSU_GOLD',
        'rotation_target_score': 70, 'rotation_reason': 'same_sector',
        'rotation_sell_qty': 5, 'rotation_freed_inr': 450.0,
        'rotation_buy_qty': 4, 'rotation_buy_price': 112.0,
    }
    with patch.object(pa.db, 'insert_portfolio_advice_snapshot',
                      side_effect=lambda rows: len(rows)) as ins, \
         patch.object(pa.db, 'get_official_advice_for_date',
                      return_value=[official_row]), \
         patch.object(pa.db, 'get_tradebook', return_value=[]):
        pa.run_advisor_lite(md)
    row = ins.call_args.args[0][0]
    assert row['rotation_target_symbol'] == 'PSU_GOLD'
    assert row['rotation_target_score'] == 70
    assert row['rotation_buy_qty'] == 4


def test_run_advisor_lite_no_holdings():
    md = MagicMock()
    md.kite.get_holdings.return_value = []
    assert pa.run_advisor_lite(md) == 0


def test_run_advisor_lite_skips_zero_qty_and_survives_symbol_failure():
    md = MagicMock()
    md.kite.get_holdings.return_value = [
        {'tradingsymbol': 'SOLD', 'quantity': 0},
        {'tradingsymbol': 'BROKEN', 'quantity': 5, 'average_price': 10,
         'last_price': 9},
        {'tradingsymbol': 'GOOD', 'quantity': 5, 'average_price': 10,
         'last_price': 12},
    ]
    good = _candles(250, start=10, step=0.05)

    def candles_for(key, interval, days):
        if 'BROKEN' in key:
            raise RuntimeError('boom')
        return good

    md.get_candles.side_effect = candles_for
    with patch.object(pa.db, 'insert_portfolio_advice_snapshot',
                      side_effect=lambda rows: len(rows)), \
         patch.object(pa.db, 'get_official_advice_for_date', return_value=[]), \
         patch.object(pa.db, 'get_tradebook', return_value=[]):
        n = pa.run_advisor_lite(md)
    assert n == 1   # only GOOD; SOLD skipped, BROKEN failed but didn't abort
