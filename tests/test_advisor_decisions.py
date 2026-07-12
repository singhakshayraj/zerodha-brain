"""Rotation sizing + Telegram Accept/Decline decision recording.
DECISIONS ONLY — pins that the bot module has no order path at all."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import advisor_bot as bot
import config
import portfolio_advisor as pa


# ── size_rotation ────────────────────────────────────────────────────────────

def test_size_full_exit():
    s = pa.size_rotation('SELL', qty=39, last_price=339.5, target_price=421.0)
    assert s['rotation_sell_qty'] == 39
    assert s['rotation_freed_inr'] == 13240.5
    # 13240.5 * 0.95 // 421 = 29
    assert s['rotation_buy_qty'] == 29
    assert s['rotation_buy_price'] == 421.0


def test_size_trim_is_half():
    s = pa.size_rotation('TRIM', qty=39, last_price=100.0, target_price=50.0)
    assert s['rotation_sell_qty'] == 19          # 39 // 2
    assert s['rotation_freed_inr'] == 1900.0
    assert s['rotation_buy_qty'] == 36           # 1900*0.95 // 50
    # TRIM of a single share still sizes to 1, never 0
    assert pa.size_rotation('TRIM', 1, 100.0, 50.0)['rotation_sell_qty'] == 1


def test_size_unsizeable_and_no_target_price():
    assert pa.size_rotation('SELL', 0, 100.0, 50.0) == {}
    assert pa.size_rotation('SELL', 10, 0, 50.0) == {}
    s = pa.size_rotation('SELL', 10, 100.0, None)
    assert s['rotation_freed_inr'] == 1000.0
    assert 'rotation_buy_qty' not in s           # sell leg still sized


# ── decision keyboard ────────────────────────────────────────────────────────

def _row(sym, verdict='SELL', score=-50, **kw):
    return {'symbol': sym, 'verdict': verdict, 'trend_score': score, **kw}


def test_keyboard_actionable_only_worst_first():
    rows = [_row('AAA', 'HOLD', 80), _row('BBB', 'SELL', -60),
            _row('CCC', 'TRIM', -10)]
    kb = pa.build_decision_keyboard(rows, '2026-07-14')
    assert [r[0]['text'] for r in kb['inline_keyboard']] == ['✅ BBB', '✅ CCC']
    assert kb['inline_keyboard'][0][1]['callback_data'] == \
        'adv|2026-07-14|BBB|decline'


def test_keyboard_none_when_all_hold():
    assert pa.build_decision_keyboard([_row('AAA', 'HOLD', 80)], '2026-07-14') is None
    assert pa.build_decision_keyboard([], '2026-07-14') is None


def test_digest_includes_sizing_line():
    rows = [_row('NTPC', 'SELL_ON_BOUNCE', -69, pnl_percent=-5.0,
                 rotation_target_symbol='ACMESOLAR', rotation_target_score=96,
                 rotation_reason='same_sector', rotation_sell_qty=39,
                 rotation_freed_inr=13240.0, rotation_buy_qty=29,
                 rotation_buy_price=421.0)]
    text = pa.build_digest(rows, '2026-07-14')
    assert 'sell 39' in text and '₹13,240' in text and '~29 ACMESOLAR' in text


# ── callback parsing + security ──────────────────────────────────────────────

def test_parse_callback():
    assert bot.parse_callback('adv|2026-07-14|NTPC|accept') == \
        ('2026-07-14', 'NTPC', 'accept')
    assert bot.parse_callback('adv|2026-07-14|NTPC|decline')[2] == 'decline'
    for bad in ('adv|d|s|buy', 'other|d|s|accept', 'adv|d|accept',
                '', None, 'adv|||accept'):
        assert bot.parse_callback(bad) is None


def _cq(chat_id='1721064751', data='adv|2026-07-14|NTPC|accept'):
    return {'update_id': 7, 'callback_query': {
        'id': 'cb1', 'data': data,
        'message': {'chat': {'id': int(chat_id)}}}}


def test_handle_update_records_decision():
    with patch.object(config, 'ADVISOR_TELEGRAM_CHAT_ID', '1721064751'), \
         patch.object(bot.db, 'record_advice_decision',
                      return_value=True) as rec, \
         patch.object(bot.telegram, 'answer_callback') as ack:
        assert bot.handle_update(_cq()) is True
    rec.assert_called_once_with('2026-07-14', 'NTPC', 'accept')
    assert 'no order placed' in ack.call_args.args[2]


def test_handle_update_rejects_foreign_chat():
    with patch.object(config, 'ADVISOR_TELEGRAM_CHAT_ID', '1721064751'), \
         patch.object(bot.db, 'record_advice_decision') as rec, \
         patch.object(bot.telegram, 'answer_callback'):
        assert bot.handle_update(_cq(chat_id='999')) is False
    rec.assert_not_called()


def test_handle_update_ignores_non_callback_and_garbage():
    with patch.object(bot.db, 'record_advice_decision') as rec, \
         patch.object(bot.telegram, 'answer_callback'):
        assert bot.handle_update({'update_id': 1, 'message': {}}) is False
        with patch.object(config, 'ADVISOR_TELEGRAM_CHAT_ID', '1721064751'):
            assert bot.handle_update(_cq(data='garbage')) is False
    rec.assert_not_called()


def test_poll_advances_offset_past_failures():
    bot._offset = None
    updates = [{'update_id': 5, 'callback_query': None},
               _cq(), ]
    with patch.object(bot.telegram, 'get_updates', return_value=updates), \
         patch.object(config, 'ADVISOR_TELEGRAM_CHAT_ID', '1721064751'), \
         patch.object(bot.db, 'record_advice_decision', return_value=True), \
         patch.object(bot.telegram, 'answer_callback'):
        n = bot._poll_once()
    assert n == 1 and bot._offset == 8   # max(update_id)+1


def test_start_refuses_unkeyed_disabled_or_qa():
    with patch.object(config, 'ADVISOR_DECISIONS_ENABLED', False):
        assert bot.start_advisor_bot() is False
    with patch.multiple(config, ADVISOR_DECISIONS_ENABLED=True,
                        ADVISOR_TELEGRAM_BOT_TOKEN='',
                        ADVISOR_TELEGRAM_CHAT_ID=''):
        assert bot.start_advisor_bot() is False
    with patch.multiple(config, ADVISOR_DECISIONS_ENABLED=True,
                        ADVISOR_TELEGRAM_BOT_TOKEN='t',
                        ADVISOR_TELEGRAM_CHAT_ID='c', QA_MODE=True):
        assert bot.start_advisor_bot() is False


def test_bot_module_has_no_order_path():
    """The decision bot cannot place an order even in principle: it never
    imports a Kite client and no attribute smells like an order method."""
    import inspect
    src = inspect.getsource(bot)
    for needle in ('KiteClient', 'place_order', 'place_buy', 'place_sell',
                   'kite_client'):
        assert needle not in src
