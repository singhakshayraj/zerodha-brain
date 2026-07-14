"""Advisor upgrades 2026-07-12b: trigger classification, asymmetric backtest
horizons, calendar-aligned alpha, price smoothing, scheduler timing/preflight.
ADVISORY ONLY — pins that no new path touches an order method."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import advisor_backtest as ab
import config
import portfolio_advisor as pa


# ── classify_trigger ─────────────────────────────────────────────────────────

def test_trigger_macro_when_ema200_agrees_with_call():
    # bearish call, price below EMA200 -> long-term structure fired it
    assert pa.classify_trigger(-45, price=90, ema200=100) == 'MACRO'
    # bullish call, price above EMA200
    assert pa.classify_trigger(60, price=110, ema200=100) == 'MACRO'


def test_trigger_micro_when_only_short_term_fired():
    # bearish call but price still ABOVE EMA200: momentum/consistency did it
    assert pa.classify_trigger(-30, price=110, ema200=100) == 'MICRO'
    # bullish call with price below EMA200: short-term bounce
    assert pa.classify_trigger(25, price=90, ema200=100) == 'MICRO'


def test_trigger_relative_strength_can_make_macro():
    # short-term bearish + decisive relative weakness = macro evidence
    assert pa.classify_trigger(-30, price=110, ema200=100,
                               rel_strength=-6.0) == 'MACRO'
    # weak rel strength isn't enough
    assert pa.classify_trigger(-30, price=110, ema200=100,
                               rel_strength=-3.0) == 'MICRO'
    # rel strength on the WRONG side never helps
    assert pa.classify_trigger(-30, price=110, ema200=100,
                               rel_strength=+8.0) == 'MICRO'


def test_trigger_missing_data_defaults_micro():
    assert pa.classify_trigger(-30, price=0, ema200=100) == 'MICRO'
    assert pa.classify_trigger(-30, price=100, ema200=None) == 'MICRO'


# ── horizon_for ──────────────────────────────────────────────────────────────

def test_horizon_micro_macro_and_legacy():
    assert ab.horizon_for({'trigger_type': 'MICRO'}) == \
        config.ADVISOR_BACKTEST_HORIZON_DAYS
    assert ab.horizon_for({'trigger_type': 'MACRO'}) == \
        config.ADVISOR_BACKTEST_MACRO_HORIZON_DAYS
    # legacy rows (pre-upgrade, no trigger_type) keep the original horizon
    assert ab.horizon_for({}) == config.ADVISOR_BACKTEST_HORIZON_DAYS
    assert ab.horizon_for({'trigger_type': None}) == \
        config.ADVISOR_BACKTEST_HORIZON_DAYS


# ── calendar-aligned index return ────────────────────────────────────────────

def _nbars(dates_closes):
    return [{'timestamp': d, 'close': c} for d, c in dates_closes]


def test_index_return_uses_exact_calendar_window():
    nifty = _nbars([('2026-07-01', 100), ('2026-07-02', 102),
                    ('2026-07-03', 104), ('2026-07-06', 106),
                    ('2026-07-07', 108)])
    # stock's horizon ended 07-03: index measured 07-01 -> 07-03, NOT its
    # own first-N-bars (which would run to 07-07 and inflate the benchmark)
    r = ab._index_return_between(nifty, '2026-07-01', '2026-07-03')
    assert round(r, 2) == 4.0


def test_index_return_none_when_window_too_thin():
    nifty = _nbars([('2026-07-01', 100)])
    assert ab._index_return_between(nifty, '2026-07-01', '2026-07-01') is None
    assert ab._index_return_between([], '2026-07-01', '2026-07-10') is None


def test_backtest_pass_asymmetric_horizons_and_alignment():
    """MACRO row not due at 10 bars stays queued; MICRO row evaluates at 10
    with the Nifty window matched to the stock's realized end date."""
    stock_bars = [{'timestamp': f'2026-06-{i + 1:02d}', 'open': 100,
                   'high': 101, 'low': 99, 'close': 100 + i, 'volume': 1}
                  for i in range(12)]
    nifty_bars = [{'timestamp': f'2026-06-{i + 1:02d}', 'open': 100,
                   'high': 101, 'low': 99, 'close': 200 + i, 'volume': 1}
                  for i in range(12)]
    micro = {'run_date': '2026-06-01', 'symbol': 'AAA', 'verdict': 'HOLD',
             'last_price': 100.0, 'trigger_type': 'MICRO', 'quantity': 10}
    macro = {'run_date': '2026-06-01', 'symbol': 'BBB', 'verdict': 'SELL',
             'last_price': 100.0, 'trigger_type': 'MACRO', 'quantity': 10}

    md = MagicMock()
    md._instrument_cache = {}
    md.get_candles.side_effect = lambda key, *a, **k: (
        nifty_bars if 'NIFTY' in key else stock_bars)

    stored = {}
    with patch.object(ab.db, 'get_unevaluated_advice',
                      return_value=[micro, macro]), \
         patch.object(ab.db, 'update_advice_outcome',
                      side_effect=lambda rd, sym, o: stored.update({sym: o}) or True):
        n = ab.run_backtest_pass(md)

    assert n == 1 and 'AAA' in stored and 'BBB' not in stored
    out = stored['AAA']
    assert out['evaluation_horizon_days'] == config.ADVISOR_BACKTEST_HORIZON_DAYS
    # stock: 100 -> bars[9].close=109 = +9%; nifty over SAME dates:
    # 200 -> 209 = +4.5%; alpha = +4.5
    assert out['outcome_return_pct'] == 9.0
    assert out['outcome_vs_nifty_pct'] == 4.5
    assert out['outcome_correct'] is True
    # no order-path method ever touched
    for name in ('place_buy_order', 'place_sell_order', 'place_order'):
        assert not getattr(md.kite, name).called


# ── price smoothing ──────────────────────────────────────────────────────────

def test_smoothed_last_price_ema3():
    md = MagicMock()
    md.get_candles.return_value = [
        {'close': 100.0, 'timestamp': '2026-07-14 09:15:00'},
        {'close': 110.0, 'timestamp': '2026-07-14 09:30:00'},
        {'close': 90.0, 'timestamp': '2026-07-14 09:45:00'}]
    # EMA(0.5): 100 -> 105 -> 97.5; one 90 flush can't drag the read to 90
    assert pa.smoothed_last_price(md, 'NSE:X', today='2026-07-14') == 97.5
    md.get_candles.assert_called_once_with('NSE:X', '15minute', 3)


def test_smoothed_last_price_never_blends_prior_session():
    """Bug fix pin (2026-07-13): the 15-min fetch spans 3 CALENDAR days; at
    09:45 only 1-2 of today's bars exist, and the old [-3:] slice blended
    Friday's close into a gapped Monday read. Prior-day bars must be
    discarded, and a day with no closed bar yet must fall back (None)."""
    md = MagicMock()
    md.get_candles.return_value = [
        {'close': 500.0, 'timestamp': '2026-07-11 15:15:00'},  # Friday
        {'close': 505.0, 'timestamp': '2026-07-11 15:30:00'},  # Friday
        {'close': 450.0, 'timestamp': '2026-07-14 09:30:00'},  # Monday gap-down
    ]
    # Only Monday's bar counts — 450.0, not an EMA polluted by 505
    assert pa.smoothed_last_price(md, 'NSE:X', today='2026-07-14') == 450.0
    # No bar closed today yet -> None (caller uses raw LTP)
    md.get_candles.return_value = [
        {'close': 505.0, 'timestamp': '2026-07-11 15:30:00'}]
    assert pa.smoothed_last_price(md, 'NSE:X', today='2026-07-14') is None


def test_smoothed_last_price_fails_safe():
    md = MagicMock()
    md.get_candles.return_value = []
    assert pa.smoothed_last_price(md, 'NSE:X') is None
    md.get_candles.side_effect = Exception('kite down')
    assert pa.smoothed_last_price(md, 'NSE:X') is None


# ── scheduler: run gate + preflight ─────────────────────────────────────────

import scheduler  # noqa: E402


def test_parse_hhmm():
    assert scheduler._parse_hhmm('09:45', (9, 20)) == (9, 45)
    assert scheduler._parse_hhmm('garbage', (9, 20)) == (9, 20)
    assert scheduler._parse_hhmm('', (9, 16)) == (9, 16)


def _at(h, m, weekday_date='2026-07-14'):   # a Tuesday
    from datetime import datetime
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    y, mo, d = (int(x) for x in weekday_date.split('-'))
    return ist.localize(datetime(y, mo, d, h, m))


def test_preflight_alerts_on_dead_token_once():
    scheduler._preflight_date = None
    with patch.object(scheduler, 'datetime') as dt, \
         patch.object(scheduler.db, 'get_enc_token', return_value='tok'), \
         patch.object(scheduler, '_token_is_live', return_value=False), \
         patch.object(config, 'ADVISOR_TELEGRAM_BOT_TOKEN', 't'), \
         patch.object(config, 'ADVISOR_TELEGRAM_CHAT_ID', 'c'), \
         patch.object(scheduler.telegram, 'send_message',
                      return_value=True) as send:
        dt.now.return_value = _at(9, 17)
        scheduler._maybe_token_preflight()
        scheduler._maybe_token_preflight()   # same day: dedup
    send.assert_called_once()
    assert 'enc_token' in send.call_args.args[2]


def test_preflight_quiet_on_live_token_and_before_time():
    scheduler._preflight_date = None
    with patch.object(scheduler, 'datetime') as dt, \
         patch.object(scheduler.db, 'get_enc_token', return_value='tok'), \
         patch.object(scheduler, '_token_is_live', return_value=True), \
         patch.object(scheduler.telegram, 'send_message') as send:
        dt.now.return_value = _at(9, 17)
        scheduler._maybe_token_preflight()
    send.assert_not_called()

    scheduler._preflight_date = None
    with patch.object(scheduler, 'datetime') as dt, \
         patch.object(scheduler.db, 'get_enc_token') as tok, \
         patch.object(scheduler.telegram, 'send_message') as send:
        dt.now.return_value = _at(9, 10)     # before 09:16
        scheduler._maybe_token_preflight()
    tok.assert_not_called()
    send.assert_not_called()


def test_preflight_skips_weekend():
    scheduler._preflight_date = None
    with patch.object(scheduler, 'datetime') as dt, \
         patch.object(scheduler.db, 'get_enc_token') as tok:
        dt.now.return_value = _at(9, 17, weekday_date='2026-07-12')  # Sunday
        scheduler._maybe_token_preflight()
    tok.assert_not_called()


# test_advisor_gate_moved_to_0945 moved to test_advisor_trigger.py
# (test_outside_window_no_official_yet_no_trigger /
# test_first_run_after_window_fires_official) — that file now owns all
# _maybe_run_advisor gating coverage with the DB-backed dedup mocks the
# 2026-07-14 rewrite requires.
