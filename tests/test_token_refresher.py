"""Unit tests for token_refresher — Kite login flow fully mocked."""
import os
from unittest.mock import MagicMock, patch

import pytest

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # force load with mock client

import config
import token_refresher


CREDS = {
    'KITE_USER_ID': 'AB1234',
    'KITE_PASSWORD': 'secret',
    'KITE_TOTP_SECRET': 'JBSWY3DPEHPK3PXP',
}


@pytest.fixture(autouse=True)
def reset_state():
    token_refresher._last_refresh_date = None
    yield
    token_refresher._last_refresh_date = None


@pytest.fixture
def creds(monkeypatch):
    for key, value in CREDS.items():
        monkeypatch.setattr(config, key, value)


def _response(status_code=200, json_body=None, text=''):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


def _session_returning(login_resp, twofa_resp=None, enctoken=None):
    session = MagicMock()
    session.post.side_effect = [login_resp] + ([twofa_resp] if twofa_resp else [])
    session.cookies.get.return_value = enctoken
    return session


class TestIsEnabled:
    def test_disabled_without_creds(self, monkeypatch):
        for key in CREDS:
            monkeypatch.setattr(config, key, None)
        assert not token_refresher.is_enabled()

    def test_disabled_with_partial_creds(self, monkeypatch, creds):
        monkeypatch.setattr(config, 'KITE_TOTP_SECRET', None)
        assert not token_refresher.is_enabled()

    def test_enabled_with_all_creds(self, creds):
        assert token_refresher.is_enabled()


class TestRefreshEncToken:
    def test_noop_when_disabled(self, monkeypatch):
        for key in CREDS:
            monkeypatch.setattr(config, key, None)
        assert token_refresher.refresh_enc_token() is None

    def test_success_writes_token_to_config(self, creds):
        login = _response(json_body={'status': 'success',
                                     'data': {'request_id': 'req-1'}})
        twofa = _response(json_body={'status': 'success'})
        session = _session_returning(login, twofa, enctoken='fresh-token')

        with patch('requests.Session', return_value=session), \
             patch.object(database, 'write_config') as write_config:
            token = token_refresher.refresh_enc_token()

        assert token == 'fresh-token'
        write_config.assert_called_once_with('enc_token', 'fresh-token')
        assert token_refresher._last_refresh_date is not None

    def test_login_failure_returns_none(self, creds):
        login = _response(status_code=403,
                          json_body={'status': 'error', 'message': 'bad creds'})
        session = _session_returning(login)

        with patch('requests.Session', return_value=session), \
             patch.object(database, 'write_config') as write_config:
            assert token_refresher.refresh_enc_token() is None

        write_config.assert_not_called()

    def test_twofa_failure_returns_none(self, creds):
        login = _response(json_body={'status': 'success',
                                     'data': {'request_id': 'req-1'}})
        twofa = _response(status_code=400, json_body={'status': 'error'})
        session = _session_returning(login, twofa)

        with patch('requests.Session', return_value=session), \
             patch.object(database, 'write_config') as write_config:
            assert token_refresher.refresh_enc_token() is None

        write_config.assert_not_called()

    def test_missing_enctoken_cookie_returns_none(self, creds):
        login = _response(json_body={'status': 'success',
                                     'data': {'request_id': 'req-1'}})
        twofa = _response(json_body={'status': 'success'})
        session = _session_returning(login, twofa, enctoken=None)

        with patch('requests.Session', return_value=session), \
             patch.object(database, 'write_config') as write_config:
            assert token_refresher.refresh_enc_token() is None

        write_config.assert_not_called()

    def test_network_error_returns_none(self, creds):
        session = MagicMock()
        session.post.side_effect = ConnectionError("kite down")

        with patch('requests.Session', return_value=session):
            assert token_refresher.refresh_enc_token() is None


class TestMaybeDailyRefresh:
    def test_noop_when_disabled(self, monkeypatch):
        for key in CREDS:
            monkeypatch.setattr(config, key, None)
        assert token_refresher.maybe_daily_refresh() is None

    def _fake_now(self, hour, minute):
        from datetime import datetime
        real = datetime.now(token_refresher.IST)
        return real.replace(hour=hour, minute=minute)

    def test_skips_before_window(self, creds):
        with patch.object(token_refresher, 'datetime') as dt:
            dt.now.return_value = self._fake_now(5, 0)
            with patch.object(token_refresher, 'refresh_enc_token') as refresh:
                token_refresher.maybe_daily_refresh()
        refresh.assert_not_called()

    def test_fires_after_window(self, creds):
        with patch.object(token_refresher, 'datetime') as dt:
            dt.now.return_value = self._fake_now(6, 30)
            with patch.object(token_refresher, 'refresh_enc_token') as refresh:
                token_refresher.maybe_daily_refresh()
        refresh.assert_called_once()

    def test_fires_only_once_per_day(self, creds):
        now = self._fake_now(7, 0)
        token_refresher._last_refresh_date = now.date()
        with patch.object(token_refresher, 'datetime') as dt:
            dt.now.return_value = now
            with patch.object(token_refresher, 'refresh_enc_token') as refresh:
                token_refresher.maybe_daily_refresh()
        refresh.assert_not_called()
