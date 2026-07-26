"""telegram.py — shared send wrapper. Policy-free: no dedup, no tiers, never
raises. Callers own their own throttling."""
from unittest.mock import MagicMock, patch

import telegram


def test_send_message_posts_and_returns_true():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    with patch('telegram.requests.post', return_value=resp) as post:
        assert telegram.send_message('tok', 'chat', 'hello') is True
    args, kwargs = post.call_args
    assert 'bottok/sendMessage' in args[0]
    assert kwargs['json'] == {'chat_id': 'chat', 'text': 'hello'}
    assert kwargs['timeout'] == 10


def test_send_message_missing_creds_short_circuits():
    with patch('telegram.requests.post') as post:
        assert telegram.send_message('', 'chat', 'x') is False
        assert telegram.send_message('tok', '', 'x') is False
        assert telegram.send_message(None, None, 'x') is False
    post.assert_not_called()


def test_send_message_network_failure_swallowed():
    with patch('telegram.requests.post', side_effect=Exception('boom')):
        assert telegram.send_message('tok', 'chat', 'x') is False


def test_send_message_http_error_swallowed():
    resp = MagicMock()
    resp.raise_for_status.side_effect = Exception('403')
    with patch('telegram.requests.post', return_value=resp):
        assert telegram.send_message('tok', 'chat', 'x') is False


def test_error_never_leaks_bot_token(capsys):
    # requests embeds the full URL (incl the secret token) in its errors —
    # the token must be scrubbed from every log line.
    tok = 'SECRET123:abcDEF'
    err = Exception(f'Conflict for url: https://api.telegram.org/bot{tok}/getUpdates')
    with patch('telegram.requests.get', side_effect=err):
        telegram.get_updates(tok)
    with patch('telegram.requests.post', side_effect=err):
        telegram.send_message(tok, 'chat', 'x')
        telegram.answer_callback(tok, 'cbid')
    out = capsys.readouterr().out
    assert tok not in out
    assert 'bot***/' in out


def test_safe_helper_scrubs_and_passes_through():
    assert telegram._safe(Exception('x/botTOK/y'), 'TOK') == 'x/bot***/y'
    assert telegram._safe(Exception('no token here'), '') == 'no token here'
