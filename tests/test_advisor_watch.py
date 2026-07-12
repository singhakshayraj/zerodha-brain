"""Intraday holdings watch (Phase 5). ADVISORY ONLY — pins no order path."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

from datetime import datetime

import pytz

import advisor_watch as aw
import config

IST = pytz.timezone('Asia/Kolkata')


def setup_function(_):
    aw._alerted.clear()


def _h(sym='ATGL', pct=None, last=724.0, prev=750.0, qty=10, avg=765.0):
    h = {'tradingsymbol': sym, 'quantity': qty, 'average_price': avg,
         'last_price': last, 'close_price': prev}
    if pct is not None:
        h['day_change_percentage'] = pct
    return h


def test_threshold_breach_alerts_once_per_day_per_direction():
    holdings = [_h(pct=-4.2)]
    first = aw.check_holdings(holdings, threshold_pct=3.0, today='2026-07-14')
    again = aw.check_holdings(holdings, threshold_pct=3.0, today='2026-07-14')
    assert len(first) == 1 and again == []
    assert 'ATGL' in first[0] and '-4.2%' in first[0]
    assert 'panic low' in first[0]                     # down-move guidance
    # same day, opposite direction is a NEW event
    assert len(aw.check_holdings([_h(pct=3.5)], 3.0, today='2026-07-14')) == 1
    # next day resets
    assert len(aw.check_holdings([_h(pct=-4.2)], 3.0, today='2026-07-15')) == 1


def test_below_threshold_silent():
    assert aw.check_holdings([_h(pct=1.2)], threshold_pct=3.0) == []


def test_day_move_computed_from_close_when_pct_missing():
    # 724 vs 750 prev close = -3.47%
    out = aw.check_holdings([_h()], threshold_pct=3.0, today='2026-07-14')
    assert len(out) == 1 and '-3.5%' in out[0]
    assert 'position -5.4%' in out[0]                  # vs avg 765


def test_unknowable_move_and_zero_qty_skipped():
    assert aw.check_holdings([_h(last=None, prev=None)], 3.0) == []
    assert aw.check_holdings([_h(pct=-9.0, qty=0)], 3.0) == []


def test_bad_holding_isolated():
    rows = [{'tradingsymbol': None}, 'garbage', _h(pct=-5.0)]
    assert len(aw.check_holdings(rows, 3.0, today='2026-07-14')) == 1


def test_market_hours_gate():
    mk = lambda h, m, d=14: IST.localize(datetime(2026, 7, d, h, m))
    assert aw._in_market_hours(mk(10, 0)) is True      # Tue mid-session
    assert aw._in_market_hours(mk(9, 0)) is False      # pre-open
    assert aw._in_market_hours(mk(15, 45)) is False    # post-close
    assert aw._in_market_hours(mk(10, 0, d=12)) is False  # Sunday


def test_start_refuses_without_flag_creds_or_in_qa():
    with patch.object(config, 'ADVISOR_INTRADAY_ALERTS_ENABLED', False):
        assert aw.start_advisor_watch() is False
    with patch.multiple(config, ADVISOR_INTRADAY_ALERTS_ENABLED=True,
                        ADVISOR_TELEGRAM_BOT_TOKEN='',
                        ADVISOR_TELEGRAM_CHAT_ID=''):
        assert aw.start_advisor_watch() is False
    with patch.multiple(config, ADVISOR_INTRADAY_ALERTS_ENABLED=True,
                        ADVISOR_TELEGRAM_BOT_TOKEN='t',
                        ADVISOR_TELEGRAM_CHAT_ID='c', QA_MODE=True):
        assert aw.start_advisor_watch() is False


def test_start_spawns_daemon_when_configured():
    with patch.multiple(config, ADVISOR_INTRADAY_ALERTS_ENABLED=True,
                        ADVISOR_TELEGRAM_BOT_TOKEN='t',
                        ADVISOR_TELEGRAM_CHAT_ID='c', QA_MODE=False), \
         patch.object(aw.threading, 'Thread') as th:
        assert aw.start_advisor_watch() is True
    assert th.call_args.kwargs['daemon'] is True
    th.return_value.start.assert_called_once()


def test_watch_never_touches_order_path():
    kite = MagicMock()
    kite.get_holdings.return_value = [_h(pct=-5.0)]
    with patch.object(aw, 'KiteClient', return_value=kite), \
         patch.object(aw.db, 'get_enc_token', return_value='tok'), \
         patch.object(aw.telegram, 'send_message', return_value=True) as send, \
         patch.object(aw, '_in_market_hours', return_value=True), \
         patch.object(aw.time, 'sleep', side_effect=KeyboardInterrupt):
        try:
            aw._watch_loop()
        except KeyboardInterrupt:
            pass
    send.assert_called_once()
    for name in ('place_buy_order', 'place_sell_order', 'place_order'):
        assert not getattr(kite, name).called


def test_watch_tick_error_never_kills_loop():
    with patch.object(aw.db, 'get_enc_token', side_effect=Exception('db down')), \
         patch.object(aw, '_in_market_hours', return_value=True), \
         patch.object(aw.time, 'sleep',
                      side_effect=[None, KeyboardInterrupt]) as slp:
        try:
            aw._watch_loop()
        except KeyboardInterrupt:
            pass
    assert slp.call_count == 2   # survived the first tick's failure
