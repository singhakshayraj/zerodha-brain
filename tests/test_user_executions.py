"""[P-25] Inferring the user's real executions from advice holdings snapshots.

The detector's job is to turn an implicit holdings time series into a record of
what was actually done. The dangerous failure is a FALSE POSITIVE — inventing a
sale the user never made — so most of these tests are about the guards.
"""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

from user_executions import detect_executions, _buy_fill_price, _followed_advice


def _snap(run, at, holdings):
    return {'run_id': run, 'run_at': at,
            'holdings': {s: {'quantity': q, 'avg_price': a, 'last_price': l}
                         for s, (q, a, l) in holdings.items()}}


def _steady(n=20):
    return {f'S{i:02d}': (10, 100.0, 105.0) for i in range(n)}


# ── the signal ──────────────────────────────────────────────────────────────

def test_full_exit_is_detected_as_a_sell():
    h = _steady(3)
    after = {k: v for k, v in h.items() if k != 'S01'}
    out = detect_executions([_snap('r1', '2026-08-06T04:30', h),
                             _snap('r2', '2026-08-06T04:36', after)])
    assert len(out) == 1
    e = out[0]
    assert (e['symbol'], e['side'], e['quantity']) == ('S01', 'SELL', 10)
    assert e['price_is_estimated'] is True     # broker leaves avg_price alone
    assert e['detected_at'] == '2026-08-06T04:36'
    assert e['prev_run_id'] == 'r1' and e['run_id'] == 'r2'


def test_partial_sell_reports_only_the_delta():
    a = _steady(2)
    b = dict(a); b['S00'] = (4, 100.0, 105.0)
    out = detect_executions([_snap('r1', 't1', a), _snap('r2', 't2', b)])
    assert [(o['symbol'], o['side'], o['quantity']) for o in out] == [('S00', 'SELL', 6)]
    assert out[0]['notes'] == 'partial sell'


def test_new_holding_is_a_buy_at_its_average_cost():
    a = _steady(2)
    b = dict(a); b['NEW'] = (50, 200.0, 205.0)
    out = detect_executions([_snap('r1', 't1', a), _snap('r2', 't2', b)])
    assert len(out) == 1
    e = out[0]
    assert (e['symbol'], e['side'], e['quantity']) == ('NEW', 'BUY', 50)
    assert e['price'] == 200.0            # avg_price IS the fill for a new name
    assert e['price_is_estimated'] is False


def test_added_position_recovers_the_exact_fill_price():
    # 10 @ 100 then +10 more, new average 120 → the added lot cost 140.
    a = {'X': (10, 100.0, 150.0)}
    b = {'X': (20, 120.0, 150.0)}
    out = detect_executions([_snap('r1', 't1', a), _snap('r2', 't2', b)])
    assert out[0]['side'] == 'BUY' and out[0]['quantity'] == 10
    assert out[0]['price'] == 140.0
    assert out[0]['price_is_estimated'] is False


def test_buy_fill_price_math():
    assert _buy_fill_price(10, 100.0, 20, 120.0) == 140.0
    assert _buy_fill_price(10, 100.0, 10, 100.0) is None    # no increase
    assert _buy_fill_price(10, None, 20, 120.0) is None     # missing average


# ── the guards (false positives are the real risk) ──────────────────────────

def test_torn_snapshot_is_not_a_mass_liquidation():
    """A failed holdings fetch returning 2 of 20 names must NOT be recorded as
    the user selling 18 positions — the single most damaging false positive."""
    full = _steady(20)
    torn = {k: v for k, v in list(full.items())[:2]}
    out = detect_executions([_snap('r1', 't1', full),
                             _snap('r2', 't2', torn),
                             _snap('r3', 't3', full)])
    assert out == []


def test_a_real_selldown_still_registers_through_the_guard():
    # Losing 3 of 20 is well above the ratio floor — must not be swallowed.
    full = _steady(20)
    fewer = {k: v for k, v in list(full.items())[3:]}
    out = detect_executions([_snap('r1', 't1', full), _snap('r2', 't2', fewer)])
    assert {o['symbol'] for o in out} == {'S00', 'S01', 'S02'}
    assert all(o['side'] == 'SELL' for o in out)


def test_torn_snapshot_diffs_against_the_last_GOOD_run():
    """After skipping a torn run, the next comparison must use the last good
    snapshot — otherwise the recovery itself looks like buying everything back."""
    full = _steady(20)
    torn = {k: v for k, v in list(full.items())[:2]}
    changed = dict(full); changed.pop('S05')
    out = detect_executions([_snap('r1', 't1', full),
                             _snap('r2', 't2', torn),
                             _snap('r3', 't3', changed)])
    assert [(o['symbol'], o['side']) for o in out] == [('S05', 'SELL')]


def test_unchanged_holdings_produce_nothing():
    h = _steady(19)
    snaps = [_snap(f'r{i}', f't{i}', h) for i in range(20)]
    assert detect_executions(snaps) == []


def test_single_snapshot_cannot_infer_anything():
    assert detect_executions([_snap('r1', 't1', _steady(5))]) == []
    assert detect_executions([]) == []


def test_leading_empty_snapshots_do_not_seed_a_baseline():
    """An empty first run must not become the baseline — everything after it
    would then read as the user buying their whole portfolio from scratch."""
    out = detect_executions([_snap('r0', 't0', {}), _snap('r1', 't1', _steady(5))])
    assert out == []


# ── advice attribution ──────────────────────────────────────────────────────

def test_followed_advice_semantics():
    assert _followed_advice('SELL', 'SELL', False) is True
    assert _followed_advice('SELL', 'TRIM', False) is True
    assert _followed_advice('SELL', 'SELL_ON_BOUNCE', False) is True
    # sold something we said to hold — a real divergence, recorded as such
    assert _followed_advice('SELL', 'HOLD', False) is False
    # bought a name we suggested rotating into
    assert _followed_advice('BUY', 'SELL', True) is True
    # bought something we never mentioned: no opinion, NOT a failure to follow
    assert _followed_advice('BUY', 'HOLD', False) is None
    assert _followed_advice('SELL', None, False) is None


def test_the_nbcc_ground_truth():
    """The real 2026-08-06 event, in the shape the live data had it: NBCC held
    at 115 across many runs, then absent. Exactly one SELL, nothing else."""
    held = {'NBCC': (115, 55.593478, 96.02), 'RVNL': (97, 427.9, 229.9),
            'ITC': (80, 384.3, 285.2), 'NTPC': (90, 342.0, 346.9)}
    gone = {k: v for k, v in held.items() if k != 'NBCC'}
    snaps = [_snap(f'r{i}', f'2026-08-06T04:{i:02d}', held) for i in range(30, 36)]
    snaps.append(_snap('r36', '2026-08-06T04:36', gone))
    out = detect_executions(snaps)
    assert len(out) == 1
    assert (out[0]['symbol'], out[0]['side'], out[0]['quantity']) == ('NBCC', 'SELL', 115)
    assert out[0]['detected_at'] == '2026-08-06T04:36'
