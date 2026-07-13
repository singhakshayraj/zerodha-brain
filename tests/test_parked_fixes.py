"""Pins for the 2026-07-13 parked-bug fixes: decision freeze after
evaluation, durable bot offset, weekly profile job."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database as db

import advisor_bot as bot
import data_jobs


# ── Fix 2: decision writes frozen once the row is evaluated ─────────────────

def test_decision_update_filters_unevaluated_rows_only():
    chain = MagicMock()
    chain.execute.return_value = MagicMock(data=[{'id': 1}])
    tbl = MagicMock()
    tbl.update.return_value.eq.return_value.eq.return_value \
        .is_.return_value = chain
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        assert db.record_advice_decision('2026-07-14', 'NTPC', 'accept') is True
    # the update chain MUST scope to evaluated_at IS NULL
    tbl.update.return_value.eq.return_value.eq.return_value \
        .is_.assert_called_once_with('evaluated_at', 'null')


def test_decision_update_rejected_when_already_evaluated():
    chain = MagicMock()
    chain.execute.return_value = MagicMock(data=[])   # filter matched 0 rows
    tbl = MagicMock()
    tbl.update.return_value.eq.return_value.eq.return_value \
        .is_.return_value = chain
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        assert db.record_advice_decision('2026-06-01', 'OLD', 'accept') is False


def test_decision_rejects_garbage_decision_value():
    assert db.record_advice_decision('2026-07-14', 'NTPC', 'buy') is False


# ── Fix 3: durable getUpdates offset ─────────────────────────────────────────

def test_offset_saved_after_poll_and_loaded_on_start():
    bot._offset = None
    upd = {'update_id': 41, 'callback_query': None}
    with patch.object(bot.telegram, 'get_updates', return_value=[upd]), \
         patch.object(bot.db, 'write_config') as wr:
        bot._poll_once()
    wr.assert_called_once_with('advisor_bot_offset', '42')

    with patch.object(bot.db, 'get_config', return_value='42'):
        bot._load_offset()
    assert bot._offset == 42
    # empty poll doesn't touch the durable marker
    with patch.object(bot.telegram, 'get_updates', return_value=[]), \
         patch.object(bot.db, 'write_config') as wr2:
        bot._poll_once()
    wr2.assert_not_called()


def test_offset_load_failure_is_safe():
    with patch.object(bot.db, 'get_config', side_effect=Exception('down')):
        bot._load_offset()
    assert bot._offset is None


# ── Fix 6: weekly profile job ────────────────────────────────────────────────

def test_weekly_profiles_once_per_iso_week():
    md = MagicMock()
    with patch.object(data_jobs, '_market_hours_now', return_value=False), \
         patch.object(data_jobs.db, 'get_config', return_value='2026-W29'), \
         patch.object(data_jobs, 'build_weekly_profiles') as build, \
         patch.object(data_jobs, 'datetime') as dt:
        dt.now.return_value.strftime.return_value = '2026-W29'
        assert data_jobs.maybe_weekly_profiles(md) == 0
    build.assert_not_called()

    with patch.object(data_jobs, '_market_hours_now', return_value=False), \
         patch.object(data_jobs.db, 'get_config', return_value='2026-W28'), \
         patch.object(data_jobs.db, 'write_config') as wr, \
         patch.object(data_jobs, 'build_weekly_profiles',
                      return_value=95) as build, \
         patch.object(data_jobs, 'datetime') as dt:
        dt.now.return_value.strftime.return_value = '2026-W29'
        assert data_jobs.maybe_weekly_profiles(md) == 95
    build.assert_called_once_with(md)
    wr.assert_called_once_with('profiles_week', '2026-W29')


def test_weekly_profiles_failure_never_raises():
    with patch.object(data_jobs.db, 'get_config',
                      side_effect=Exception('db down')):
        assert data_jobs.maybe_weekly_profiles(MagicMock()) == 0


def test_build_weekly_profiles_isolates_symbol_failures():
    md = MagicMock()
    md._instrument_cache = {}
    md.get_candles.side_effect = Exception('fetch fails for everyone')
    with patch.object(data_jobs.db, 'upsert_stock_profile'):
        n = data_jobs.build_weekly_profiles(md, asof='2026-07-13')
    # every fetch failed -> profiles still built from empty dailies via the
    # universe-average fallback path, and nothing raised
    assert isinstance(n, int)


# ── 2026-07-14 scan fixes ────────────────────────────────────────────────────

def test_weekly_profiles_refuse_market_hours():
    from datetime import datetime as _dt
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    md = MagicMock()
    in_market = ist.localize(_dt(2026, 7, 14, 10, 30))   # Tuesday mid-session
    post_close = ist.localize(_dt(2026, 7, 14, 15, 45))
    with patch.object(data_jobs, 'datetime') as dt, \
         patch.object(data_jobs.db, 'get_config', return_value='old-week'), \
         patch.object(data_jobs, 'build_weekly_profiles') as build:
        dt.now.return_value = in_market
        assert data_jobs.maybe_weekly_profiles(md) == 0
        build.assert_not_called()
        dt.now.return_value = post_close
        build.return_value = 7
        with patch.object(data_jobs.db, 'write_config'):
            assert data_jobs.maybe_weekly_profiles(md) == 7
        build.assert_called_once()


def test_profile_builder_paces_fetches():
    md = MagicMock()
    md._instrument_cache = {}
    md.get_candles.return_value = []
    with patch.object(data_jobs.time, 'sleep') as slp, \
         patch.object(data_jobs.db, 'upsert_stock_profile'):
        data_jobs.build_weekly_profiles(md, asof='2026-07-14')
    assert slp.call_count >= 50          # one pace per symbol fetch


def test_bot_loop_sleeps_on_instant_return():
    """Network-down: get_updates swallows the error and returns [] instantly.
    The loop must sleep the interval instead of spinning hot."""
    with patch.object(bot, '_poll_once', return_value=0), \
         patch.object(bot.time, 'monotonic', side_effect=[0.0, 0.1, 10.0]), \
         patch.object(bot.time, 'sleep',
                      side_effect=KeyboardInterrupt) as slp:
        try:
            bot._bot_loop()
        except KeyboardInterrupt:
            pass
    slp.assert_called_once()


def test_digest_caps_at_twelve_calls_with_overflow_line():
    import portfolio_advisor as pa
    rows = [{'symbol': f'S{i:02d}', 'verdict': 'SELL',
             'trend_score': -90 + i, 'pnl_percent': -1.0}
            for i in range(15)]
    text = pa.build_digest(rows, '2026-07-14')
    assert 'S00' in text and 'S11' in text          # worst 12 kept
    assert 'S12' not in text and 'S14' not in text  # overflow trimmed
    assert 'and 3 more' in text
    assert len(text) < 4096
    kb = pa.build_decision_keyboard(rows, '2026-07-14')
    assert len(kb['inline_keyboard']) == 12


# ── P1/P3/P4/P5 fixes (2026-07-14, second pass) ─────────────────────────────

def test_concurrent_cap_gates_entries_in_data_mode():
    import config
    from brain import TradingBrain
    b = TradingBrain()
    b.session_stats = {'trades_executed': 0, 'total_pnl': 0}
    with patch.object(config, 'data_collection_active', return_value=True):
        assert b._entry_block('AAA', 5, 0,
                              open_positions=config.DATA_MAX_CONCURRENT_POSITIONS) \
            == 'CONCURRENT_CAP'
        assert b._entry_block('AAA', 5, 0, open_positions=2) == ''
    with patch.object(config, 'data_collection_active', return_value=False):
        assert b._entry_block('AAA', 5, 0, open_positions=99) == ''


def test_completed_bars_drops_only_todays_forming_bar():
    import portfolio_advisor as pa
    candles = [{'timestamp': '2026-07-11', 'close': 1},
               {'timestamp': '2026-07-14', 'close': 2}]
    out = pa.completed_bars(candles, today='2026-07-14')
    assert [c['timestamp'] for c in out] == ['2026-07-11']
    # yesterday-only series untouched; empty safe
    assert pa.completed_bars(candles[:1], today='2026-07-14') == candles[:1]
    assert pa.completed_bars([], today='2026-07-14') == []


def test_fetch_all_pages_past_1000():
    q = MagicMock()
    pages = {0: [{'i': n} for n in range(1000)],
             1000: [{'i': n} for n in range(1000, 1500)]}
    q.range.side_effect = lambda s, e: MagicMock(
        execute=lambda: MagicMock(data=pages.get(s, [])))
    rows = db._fetch_all(q)
    assert len(rows) == 1500
    q.range.assert_any_call(0, 999)
    q.range.assert_any_call(1000, 1999)


def test_append_decision_skip_appends_and_dedups():
    sel = MagicMock()
    sel.execute.return_value = MagicMock(data=[{'skip_reasons': ['DQ_OK']}])
    upd = MagicMock()
    tbl = MagicMock()
    tbl.select.return_value.eq.return_value.limit.return_value = sel
    tbl.update.return_value.eq.return_value = upd
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        assert db.append_decision_skip('d1', 'ENTRY_DEFERRED:HOURLY_PACE') is True
    tbl.update.assert_called_once_with(
        {'skip_reasons': ['DQ_OK', 'ENTRY_DEFERRED:HOURLY_PACE']})
    # already present -> no update call
    sel.execute.return_value = MagicMock(
        data=[{'skip_reasons': ['ENTRY_DEFERRED:HOURLY_PACE']}])
    tbl.update.reset_mock()
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        assert db.append_decision_skip('d1', 'ENTRY_DEFERRED:HOURLY_PACE') is True
    tbl.update.assert_not_called()
    assert db.append_decision_skip(None, 'x') is False


def test_inplay_fallback_locks_top_names_in_data_mode():
    import config
    md = MagicMock()
    universe = {'NSE:AAA': {}, 'NSE:BBB': {}}
    stats = {'NSE:AAA': {'or_rvol': 1.4, 'or_high': 10, 'or_low': 9,
                         'gap_pct': 0.1, 'or_volume': 1, 'avg_or_volume': 1},
             'NSE:BBB': {'or_rvol': 0.9, 'or_high': 10, 'or_low': 9,
                         'gap_pct': 0.1, 'or_volume': 1, 'avg_or_volume': 1}}
    locked = {}
    with patch.object(data_jobs, '_past_lock_time', return_value=True), \
         patch.object(data_jobs.db, 'inplay_locked', return_value=False), \
         patch.object(data_jobs.inplay, 'opening_range_stats',
                      side_effect=lambda c: dict(stats[md._last_key]) if False else stats.get(getattr(md, '_k', 'NSE:AAA'))), \
         patch.object(data_jobs.db, 'lock_inplay_list',
                      side_effect=lambda d, r: locked.update({d: r}) or len(r)):
        pass  # direct path below instead — opening stats via get_candles key
    # simpler: call rank-level behavior through maybe_lock_inplay with
    # per-key stats delivered by a stateful fake
    keys = iter(['NSE:AAA', 'NSE:BBB'])
    def fake_stats(_c):
        return dict(stats[next(keys)])
    with patch.object(data_jobs, '_past_lock_time', return_value=True), \
         patch.object(data_jobs.db, 'inplay_locked', return_value=False), \
         patch.object(data_jobs.inplay, 'opening_range_stats',
                      side_effect=fake_stats), \
         patch.object(config, 'data_collection_active', return_value=True), \
         patch.object(data_jobs.db, 'lock_inplay_list',
                      side_effect=lambda d, r: locked.update({d: r}) or len(r)):
        n = data_jobs.maybe_lock_inplay(md, universe)
    assert n == 2                       # both below bar 2.0, fallback locked
    rows = list(locked.values())[0]
    assert rows[0]['symbol'] == 'NSE:AAA' and rows[0]['or_rvol'] == 1.4


def test_inplay_no_fallback_outside_data_mode():
    import config
    md = MagicMock()
    universe = {'NSE:AAA': {}}
    def fake_stats(_c):
        return {'or_rvol': 1.4, 'or_high': 10, 'or_low': 9, 'gap_pct': 0.1,
                'or_volume': 1, 'avg_or_volume': 1}
    with patch.object(data_jobs, '_past_lock_time', return_value=True), \
         patch.object(data_jobs.db, 'inplay_locked', return_value=False), \
         patch.object(data_jobs.inplay, 'opening_range_stats',
                      side_effect=fake_stats), \
         patch.object(config, 'data_collection_active', return_value=False), \
         patch.object(data_jobs.db, 'lock_inplay_list') as lock:
        assert data_jobs.maybe_lock_inplay(md, universe) == 0
    lock.assert_not_called()
