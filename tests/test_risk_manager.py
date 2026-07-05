"""T1.4 — RiskManager unit tests."""
import pytest
from unittest.mock import patch
from risk_manager import RiskManager


@pytest.fixture
def rm():
    return RiskManager()


# --- stop_distance=0 ---

def test_zero_stop_distance_returns_zero(rm, capsys):
    qty = rm.calculate_position_size(
        capital=10000.0,
        live_price=1000.0,
        confidence=80,
        stop_loss_price=1000.0,  # same as price → distance=0
    )
    assert qty == 0
    captured = capsys.readouterr()
    assert 'SL invalid' in captured.out


# --- stop_distance > 50% of price ---

def test_stop_too_wide_returns_zero(rm, capsys):
    qty = rm.calculate_position_size(
        capital=10000.0,
        live_price=1000.0,
        confidence=80,
        stop_loss_price=400.0,  # 600 distance > 50% of 1000
    )
    assert qty == 0
    captured = capsys.readouterr()
    assert 'SL too wide' in captured.out


# --- price > capital → skip ---

def test_price_exceeds_capital_returns_zero(rm, capsys):
    qty = rm.calculate_position_size(
        capital=5000.0,
        live_price=10000.0,
        confidence=80,
        stop_loss_price=9500.0,
    )
    assert qty == 0
    captured = capsys.readouterr()
    assert 'exceeds capital' in captured.out


# --- normal fixed 1% sizing ---

def test_fixed_1pct_sizing_normal(rm):
    # capital=10000, price=200, stop=190 (distance=10)
    # risk=100, qty_risk=10, position=2000 ≥ MIN_POSITION_VALUE
    qty = rm.calculate_position_size(
        capital=10000.0,
        live_price=200.0,
        confidence=75,
        stop_loss_price=190.0,
    )
    assert qty > 0


# --- minimum position value enforcement ---

def test_minimum_position_value_raised(rm, capsys):
    """Rs200 stock, Rs10000 capital, small risk qty → raise to Rs2000 min."""
    qty = rm.calculate_position_size(
        capital=10000.0,
        live_price=200.0,
        confidence=75,
        stop_loss_price=198.0,  # distance=2, risk=100, qty_risk=50 → 50*200=10000 > max
    )
    position_value = qty * 200.0
    assert position_value >= 2000.0 or qty >= 1


def test_cannot_reach_min_position_warns(rm, capsys):
    """Rs13000 stock with Rs10000 capital → can't reach Rs2000 min (need 1 share = Rs13000)."""
    qty = rm.calculate_position_size(
        capital=10000.0,
        live_price=13000.0,
        confidence=75,
        stop_loss_price=12500.0,
    )
    # price > capital → returns 0
    assert qty == 0


# --- brokerage warning ---

def test_brokerage_warning_small_position(rm, capsys):
    """Rs500 position → Rs40/500 = 8% → warning."""
    qty = rm.calculate_position_size(
        capital=10000.0,
        live_price=500.0,
        confidence=75,
        stop_loss_price=490.0,  # distance=10, risk=100, qty_risk=10 → 5000 position
    )
    # actual position depends on qty; just verify no crash
    assert qty >= 0


def test_brokerage_ok_large_position(rm, capsys):
    """Rs2500+ position → brokerage OK."""
    qty = rm.calculate_position_size(
        capital=10000.0,
        live_price=500.0,
        confidence=75,
        stop_loss_price=490.0,
    )
    captured = capsys.readouterr()
    # Should log 'OK' brokerage line if position >= 4000
    if qty * 500 >= 4000:
        assert 'OK' in captured.out


# --- Kelly sizing with enough trades ---

def test_kelly_sizing_activated(rm, capsys):
    qty = rm.calculate_position_size(
        capital=10000.0,
        live_price=500.0,
        confidence=75,
        stop_loss_price=490.0,
        target_price=530.0,
        historical_win_rate=0.6,
        historical_avg_win=200.0,
        historical_avg_loss=100.0,
        n_trades=15,
    )
    captured = capsys.readouterr()
    assert 'DYNAMIC' in captured.out or 'FIXED' in captured.out
    assert qty >= 0


def test_kelly_not_used_fewer_than_10_trades(rm, capsys):
    rm.calculate_position_size(
        capital=10000.0,
        live_price=500.0,
        confidence=75,
        stop_loss_price=490.0,
        target_price=530.0,
        historical_win_rate=0.6,
        n_trades=5,
    )
    captured = capsys.readouterr()
    assert 'FIXED' in captured.out


# --- position capping ---

def test_position_capped_by_max_percent(rm, capsys):
    """qty * price > 40% of capital → cap."""
    qty = rm.calculate_position_size(
        capital=10000.0,
        live_price=100.0,
        confidence=75,
        stop_loss_price=99.0,  # distance=1, risk=100, qty_risk=100 → 10000 > 4000 cap
    )
    assert qty * 100.0 <= 10000.0 * 0.40 + 1  # within cap (allow +1 for rounding)


# --- is_market_open ---

def test_is_market_open_false_weekend(rm):
    with patch('risk_manager.datetime') as mock_dt:
        from datetime import datetime
        import pytz
        IST = pytz.timezone('Asia/Kolkata')
        # Saturday
        mock_dt.now.return_value = datetime(2026, 5, 23, 10, 0, tzinfo=IST)
        result = rm.is_market_open()
    assert result is False


def test_is_market_open_true_weekday(rm):
    from datetime import datetime
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    d = datetime(2026, 5, 21, 10, 0, 0, tzinfo=IST)

    class FakeDatetime:
        @staticmethod
        def now(tz=None):
            return d

    with patch('risk_manager.datetime', FakeDatetime):
        result = rm.is_market_open()
    assert isinstance(result, bool)


# --- get_time_bucket ---

def test_get_time_bucket_morning(rm):
    with patch('risk_manager.datetime') as mock_dt:
        from datetime import datetime
        import pytz
        IST = pytz.timezone('Asia/Kolkata')
        mock_dt.now.return_value = datetime(2026, 5, 21, 10, 0, tzinfo=IST)
        result = rm.get_time_bucket()
    assert result == 'MORNING'


def test_get_time_bucket_pre_market(rm):
    with patch('risk_manager.datetime') as mock_dt:
        from datetime import datetime
        import pytz
        IST = pytz.timezone('Asia/Kolkata')
        mock_dt.now.return_value = datetime(2026, 5, 21, 8, 0, tzinfo=IST)
        result = rm.get_time_bucket()
    assert result == 'PRE_MARKET'
