"""Trial 261 sector layer: fixed rules on top of v1."""
import sector_tilt as st


def test_rules():
    assert st.tilt('SELL', -50, 40) == ('TRIM', 'strong_sector_softens_sell')
    assert st.tilt('SELL_ON_BOUNCE', -50, 30) == ('TRIM', 'strong_sector_softens_sell')
    assert st.tilt('TRIM', 10, 35) == ('HOLD', 'strong_sector_keeps_trim')
    assert st.tilt('TRIM', -10, -35) == ('SELL', 'weak_sector_hardens_trim')
    assert st.tilt('HOLD', -5, -40) == ('TRIM', 'weak_sector_trims_hold')
    assert st.tilt('HOLD', 5, -40) == ('HOLD', None)          # stock itself fine
    assert st.tilt('SELL', -50, 29) == ('SELL', None)         # sector not strong enough
    assert st.tilt('INSUFFICIENT', 0, 90) == ('INSUFFICIENT', None)
    assert st.tilt('SELL', -50, None) == ('SELL', None)


def test_small_groups_never_tilt_and_v1_untouched():
    items = [{'symbol': f'S{i}', 'trend_score': 50} for i in range(5)] + \
            [{'symbol': 'T0', 'trend_score': 50}]
    sector_of = {**{f'S{i}': 'IT' for i in range(5)}, 'T0': 'Media'}
    trends = st.sector_trends(items, sector_of)
    assert trends == {'Information Technology': 50} or trends == {'IT': 50}
    rows = [{'symbol': 'S0', 'verdict': 'SELL', 'trend_score': -40},
            {'symbol': 'T0', 'verdict': 'SELL', 'trend_score': -40}]
    assert st.apply(rows, trends, sector_of) == 1
    assert rows[0]['verdict'] == 'SELL' and rows[0]['sector_verdict'] == 'TRIM'
    assert rows[1]['sector_verdict'] == 'SELL' and rows[1]['sector_rule'] is None


def test_compare_edge_values_half_exit_and_skips_identical():
    import sys; sys.path.insert(0, 'scripts')
    import compare_sector_tilt as c
    rows = [{'run_date': '2026-11-02', 'verdict': 'SELL', 'sector_verdict': 'TRIM', 'outcome_vs_nifty_pct': 4.0},
            {'run_date': '2026-11-02', 'verdict': 'HOLD', 'sector_verdict': 'TRIM', 'outcome_vs_nifty_pct': -2.0},
            {'run_date': '2026-11-02', 'verdict': 'HOLD', 'sector_verdict': 'HOLD', 'outcome_vs_nifty_pct': 9.0},
            {'run_date': '2026-11-03', 'verdict': 'SELL', 'sector_verdict': 'TRIM', 'outcome_vs_nifty_pct': None}]
    assert c.edges_by_date(rows) == {'2026-11-02': [2.0, 1.0]}
