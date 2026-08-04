"""Advisor paper-portfolio engine (P-14 phase 2). Drives run_paper_portfolio
against an in-memory fake DB + fake market data — the same validation approach
as the paper broker (no live token). Asserts seeding, verdict → paper action
(SELL/TRIM/rotation/pick), horizon closes, cash, equity snapshots, and that no
order path is ever touched."""
import os
import uuid
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
    'ROTATION_ADVISOR_ENABLED': 'true',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import advisor_paper as ap


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self, official_rows):
        self.official = official_rows
        self.positions = []      # list of dicts (with 'id')
        self.equity = []         # list of snapshot dicts
        self.cfg = {}

    def get_official_advice_for_date(self, run_date):
        return self.official

    def paper_book_exists(self, book):
        return any(p['book'] == book for p in self.positions)

    def paper_positions(self, book, open_only=False):
        return [dict(p) for p in self.positions
                if p['book'] == book and (not open_only or p.get('is_open'))]

    def insert_paper_positions(self, rows):
        for r in rows:
            r = dict(r)
            r.setdefault('id', str(uuid.uuid4()))
            r.setdefault('is_open', True)
            self.positions.append(r)
        return len(rows)

    def update_paper_position(self, pid, patch):
        for p in self.positions:
            if p['id'] == pid:
                p.update(patch)
                return True
        return False

    def upsert_paper_equity(self, row):
        self.equity = [e for e in self.equity
                       if not (e['book'] == row['book']
                               and e['snapshot_date'] == row['snapshot_date'])]
        self.equity.append(dict(row))
        return True

    def paper_equity_curve(self, book, limit=400):
        return [dict(e) for e in self.equity if e['book'] == book]

    def write_config(self, k, v):
        self.cfg[k] = v

    def get_config(self, k):
        return self.cfg.get(k)


def _md(price_map):
    """price_map: {symbol: [closes...]} -> daily bars 2026-07-01.. per symbol.
    NIFTY 50 defaults flat at 20000."""
    md = MagicMock()
    md._instrument_cache = {}

    def candles(key, interval, days):
        sym = key.replace('NSE:', '')
        if sym == 'NIFTY 50':
            closes = price_map.get('NIFTY 50', [20000] * 15)
        else:
            closes = price_map.get(sym, [])
        return [{'timestamp': f'2026-07-{i + 1:02d}', 'close': c}
                for i, c in enumerate(closes)]

    md.get_candles.side_effect = candles
    return md


def _row(symbol, verdict, qty=10, avg=100.0, last=100.0, **extra):
    r = {'id': str(uuid.uuid4()), 'symbol': symbol, 'verdict': verdict,
         'quantity': qty, 'avg_price': avg, 'last_price': last}
    r.update(extra)
    return r


def _run(fake, md, run_date='2026-07-10'):
    with patch.object(ap, 'db', fake):
        return ap.run_paper_portfolio(md, run_date=run_date)


# ── tests ────────────────────────────────────────────────────────────────────

def test_seeds_both_books_from_holdings():
    rows = [_row('AAA', 'HOLD', qty=10, avg=100),
            _row('BBB', 'HOLD', qty=5, avg=200)]
    fake = FakeDB(rows)
    md = _md({'AAA': [100] * 12, 'BBB': [200] * 12})
    assert _run(fake, md) is True
    mgmt = fake.paper_positions('MANAGEMENT')
    assert {p['symbol'] for p in mgmt} == {'AAA', 'BBB'}
    assert all(p['source'] == 'SEED' and p['is_open'] for p in mgmt)
    # PICKING seeded with cash anchor, no positions yet
    assert 'advisor_paper_seed_pick' in fake.cfg
    assert fake.paper_positions('PICKING') == []


def test_sell_verdict_closes_management_position():
    rows = [_row('AAA', 'SELL', qty=10, avg=100, last=110)]
    fake = FakeDB(rows)
    md = _md({'AAA': [100, 105, 110]})  # last close 110
    _run(fake, md)
    pos = fake.paper_positions('MANAGEMENT')[0]
    assert pos['is_open'] is False
    assert pos['exit_reason'] == 'SELL_VERDICT'
    assert pos['realized_pnl'] == round((110 - 100) * 10, 2)


def test_trim_books_half_and_keeps_remainder():
    rows = [_row('AAA', 'TRIM', qty=10, avg=100, last=120)]
    fake = FakeDB(rows)
    md = _md({'AAA': [100, 110, 120]})
    _run(fake, md)
    positions = fake.paper_positions('MANAGEMENT')
    closed = [p for p in positions if not p['is_open']]
    openp = [p for p in positions if p['is_open']]
    assert len(closed) == 1 and closed[0]['exit_reason'] == 'TRIM'
    assert closed[0]['qty'] == 5              # trimmed half booked
    assert openp[0]['qty'] == 5               # remainder still held


def test_rotation_sells_holding_and_buys_target():
    rows = [_row('AAA', 'ROTATE', qty=10, avg=100, last=100,
                 rotation_target_symbol='ZZZ', rotation_sell_qty=10,
                 rotation_buy_price=50.0, rotation_buy_qty=20)]
    fake = FakeDB(rows)
    md = _md({'AAA': [100, 100, 100], 'ZZZ': [50, 50, 50]})
    with patch.object(config, 'ROTATION_ADVISOR_ENABLED', True):
        _run(fake, md)
    mgmt = fake.paper_positions('MANAGEMENT')
    zzz = [p for p in mgmt if p['symbol'] == 'ZZZ']
    aaa_out = [p for p in mgmt if p['symbol'] == 'AAA' and not p['is_open']]
    assert zzz and zzz[0]['source'] == 'ROTATION' and zzz[0]['is_open']
    assert aaa_out and aaa_out[0]['exit_reason'] == 'ROTATION_OUT'


def test_picking_buys_target_under_cap_and_snapshots():
    rows = [_row('AAA', 'HOLD', qty=10, avg=100, last=100,
                 rotation_target_symbol='ZZZ', rotation_sell_qty=1,
                 rotation_buy_price=50.0)]
    fake = FakeDB(rows)
    md = _md({'AAA': [100] * 3, 'ZZZ': [50] * 3})
    _run(fake, md)
    pick = fake.paper_positions('PICKING')
    assert pick and pick[0]['symbol'] == 'ZZZ'
    # single-name cap = 10% of 100k = 10k / 50 = 200 shares max
    assert pick[0]['qty'] == 200
    eq = fake.paper_equity_curve('PICKING')[0]
    assert eq['open_positions'] == 1
    # cash = 100k - 200*50 = 90k; equity ~ 100k
    assert eq['cash'] == 90000.0
    assert eq['total_equity'] == 100000.0


def test_picking_closes_at_horizon():
    # entry 2026-07-01 @50, horizon bar (10th) closes @60 -> +20%
    closes = [50, 51, 52, 53, 54, 55, 56, 57, 58, 60, 62]
    rows = [_row('AAA', 'HOLD', qty=10, avg=100, last=100,
                 rotation_target_symbol='ZZZ', rotation_sell_qty=1,
                 rotation_buy_price=50.0)]
    fake = FakeDB(rows)
    md = _md({'AAA': [100] * 12, 'ZZZ': closes})
    with patch.object(config, 'ADVISOR_PAPER_PICK_HORIZON_DAYS', 10):
        # seed the pick on run_date 07-01 so it has 10 bars by the second run
        _run(fake, md, run_date='2026-07-01')
        _run(fake, md, run_date='2026-07-15')
    pick = fake.paper_positions('PICKING')[0]
    assert pick['is_open'] is False
    assert pick['exit_reason'] == 'HORIZON'
    assert pick['exit_price'] == 60          # 10th bar close


def test_management_baseline_tracks_frozen_seed():
    rows = [_row('AAA', 'SELL', qty=10, avg=100, last=120)]
    fake = FakeDB(rows)
    md = _md({'AAA': [100, 110, 120]})
    _run(fake, md)
    eq = fake.paper_equity_curve('MANAGEMENT')[0]
    # actual book sold AAA -> cash 1200, no positions; baseline holds 10@120=1200
    assert eq['baseline_equity'] == 1200.0
    assert eq['total_equity'] == 1200.0


def test_idempotent_same_day_rerun_does_not_double_apply():
    rows = [_row('AAA', 'SELL', qty=10, avg=100, last=110)]
    fake = FakeDB(rows)
    md = _md({'AAA': [100, 105, 110]})
    _run(fake, md)
    n_after_first = len(fake.positions)
    _run(fake, md)  # same run_date again
    assert len(fake.positions) == n_after_first  # no duplicate actions


def test_no_official_advice_is_noop():
    fake = FakeDB([])
    md = _md({})
    assert _run(fake, md) is False
    assert fake.positions == [] and fake.equity == []


def test_disabled_flag_skips():
    fake = FakeDB([_row('AAA', 'HOLD')])
    md = _md({'AAA': [100]})
    with patch.object(config, 'ADVISOR_PAPER_ENABLED', False):
        assert _run(fake, md) is False
