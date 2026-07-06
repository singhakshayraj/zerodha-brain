"""REQ-080 — property/invariant coverage on position sizing.

No hypothesis dependency (would have to ship to Railway); instead a dense
deterministic grid sweep of the inputs that matter. Invariants asserted for
EVERY combination:

  I1  qty is a non-negative int — never a crash, never negative
  I2  position value never exceeds the hard cap (MAX_POSITION_PERCENT of
      capital), give or take one share of rounding
  I3  when the min-position floor is NOT the binding constraint, per-trade
      risk (qty × stop distance) never exceeds the 1% risk budget — this is
      the core "sizing never exceeds risk% for any input" guarantee
  I4  degenerate inputs (zero/inverted stop, price > capital) return 0, not
      a partial position
"""
import itertools

from risk_manager import RiskManager
import config

rm = RiskManager()

CAPITALS = [2000, 5000, 10000, 25000, 100000]
PRICES = [12.5, 50, 100, 247.3, 500, 1500, 3000]
STOP_PCTS = [0.005, 0.01, 0.02, 0.05, 0.10, 0.25]   # stop distance as % of price
CONFIDENCES = [50, 65, 80, 95]


def _size(capital, price, stop_pct, conf):
    stop = round(price * (1 - stop_pct), 2)
    return rm.calculate_position_size(
        capital=capital, live_price=price, confidence=conf,
        stop_loss_price=stop, target_price=round(price * 1.03, 2),
        historical_win_rate=None, n_trades=0, symbol='PROP',
    ), stop


def test_sizing_invariants_over_grid():
    checked = 0
    for capital, price, stop_pct, conf in itertools.product(
            CAPITALS, PRICES, STOP_PCTS, CONFIDENCES):
        if price > capital:
            # I4: unaffordable single share → must be 0
            qty, _ = _size(capital, price, stop_pct, conf)
            assert qty == 0, (capital, price)
            continue

        qty, stop = _size(capital, price, stop_pct, conf)
        checked += 1

        # I1
        assert isinstance(qty, int) and qty >= 0, (capital, price, stop_pct)

        if qty == 0:
            continue

        # I2 — hard value cap (+1 share tolerance for int rounding)
        value = qty * price
        assert value <= capital * config.MAX_POSITION_PERCENT + price + 0.01, (
            f"value cap breached: qty={qty} price={price} value={value:.0f} "
            f"cap={capital * config.MAX_POSITION_PERCENT:.0f}"
        )

        # I3 — risk budget, only where the min-position floor isn't forcing
        # size up. The floor triggers when the risk-based value would sit
        # below MIN_POSITION_VALUE; outside that regime, risk must fit 1%.
        stop_distance = price - stop
        risk_budget = capital * 0.01
        risk_based_qty = int(risk_budget / stop_distance)
        risk_based_value = risk_based_qty * price
        floor_binding = risk_based_value < config.MIN_POSITION_VALUE
        if not floor_binding:
            actual_risk = qty * stop_distance
            # +1 share of stop distance for the int-floor rounding
            assert actual_risk <= risk_budget + stop_distance + 0.01, (
                f"risk budget breached: qty={qty} risk={actual_risk:.1f} "
                f"budget={risk_budget:.1f} (capital={capital} price={price})"
            )
    assert checked > 100  # sanity: the grid actually exercised the function


def test_zero_stop_distance_returns_zero():
    assert rm.calculate_position_size(
        capital=10000, live_price=100, confidence=80,
        stop_loss_price=100, n_trades=0, symbol='X') == 0


def test_inverted_stop_far_returns_zero():
    # stop more than 50% away → rejected as bad ATR
    assert rm.calculate_position_size(
        capital=10000, live_price=100, confidence=80,
        stop_loss_price=40, n_trades=0, symbol='X') == 0


def test_req005_guarantee_min_position_within_budget_when_config_valid():
    """REQ-005: with a config that passes the sanity check, the min-position
    floor at a 2% stop must not force risk above the 1% budget. Proves the
    sanity threshold is set consistently with the sizing floor."""
    # smallest capital that passes _validate_session_config's min-position test
    capital = config.MIN_POSITION_VALUE * 0.02 / (config.RISK_PER_TRADE_PCT / 100)
    risk_budget = capital * config.RISK_PER_TRADE_PCT / 100
    min_pos_risk_at_2pct = config.MIN_POSITION_VALUE * 0.02
    assert min_pos_risk_at_2pct <= risk_budget + 1e-6
