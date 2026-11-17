"""[P-55] Morning brief: six lines at most, numbers from the inputs."""
import json
from unittest.mock import patch

import morning_brief as mb
from tests.fake_supabase import FakeSupabase

PRIMARY = [{'symbol': 'ATGL', 'verdict': 'SELL', 'rotation_target_symbol': 'PETRONET'},
           {'symbol': 'IOB', 'verdict': 'TRIM', 'rotation_target_symbol': None},
           {'symbol': 'INFY', 'verdict': 'HOLD', 'rotation_target_symbol': None}]
PRIOR = [{'symbol': 'ATGL', 'verdict': 'HOLD'}, {'symbol': 'IOB', 'verdict': 'TRIM'}]
NATWAR = {'run_date': '2026-11-16', 'rows': [{'symbol': 'SCI', 'verdict': 'SELL', 'rotation_target_symbol': 'IGIL'}],
          'sector_view': {'groups': [{'group': 'PSU banks', 'my_weight_pct': 16.4, 'index_weight_pct': 4.4}]}}
MACRO = {'date': '2026-11-13', 'data': {'nifty': {'close': 25299.35, 'd1': 0.18, 'd5': 3.11},
                                        'vix': {'close': 16.62}, 'breadth': {'pct_trend_up': 49}, 'fii_net_cr': None}}


def test_compose_lines():
    lines = mb.compose('2026-11-16', PRIMARY, PRIOR, '2026-11-13', NATWAR,
                       {'Kanaklata': {'groups': []}, 'Natwar': NATWAR['sector_view']}, MACRO, '2026-10-01')
    by = {l['kind']: l['text'] for l in lines}
    assert by['moves'] == 'Kanaklata: 1 sell, 1 trim → ATGL→PETRONET · Natwar: 1 sell, 0 trim → SCI→IGIL'
    assert by['changes'] == 'Since 2026-11-13: ATGL HOLD→SELL, +INFY new'
    assert by['household'] == 'Overweight: PSU banks 16% vs 4% (Natwar)'
    assert by['tape'] == 'Tape 2026-11-13: Nifty 25,299 (+0.2%, 5d +3.1%) · VIX 16.62 · breadth 49% up · FII —'
    assert by['stale'] == 'Stale: no FII/DII row (is the 21:30 fetch job loaded?); fundamentals 2026-10-01'
    assert len(lines) <= 6


def test_stale_flags_old_tape_and_missing_natwar():
    old = {**MACRO, 'date': '2026-11-10'}
    lines = mb.compose('2026-11-16', [], [], None, {'run_date': '2026-11-13', 'rows': []}, {}, old, None)
    stale = [l for l in lines if l['kind'] == 'stale'][0]
    assert stale['warn'] and 'tape data stops at 2026-11-10' in stale['text']
    assert 'Natwar last ran 2026-11-13' in stale['text']
    assert mb.prev_trading_day('2026-11-16') == '2026-11-13'       # Monday -> Friday
    six = [{'symbol': f'S{i}', 'verdict': 'SELL', 'rotation_target_symbol': 'T'} for i in range(6)]
    assert mb._moves('K', six).endswith('S3→T, +2 more')


def test_publish_reads_official_rows_and_writes_config():
    fake = FakeSupabase()
    fake.tables['portfolio_advice'] = [
        {**r, 'run_date': '2026-11-16', 'is_official': True} for r in PRIMARY
    ] + [{**r, 'run_date': '2026-11-13', 'is_official': True} for r in PRIOR
    ] + [{'symbol': 'ZZZ', 'verdict': 'SELL', 'run_date': '2026-11-16', 'is_official': False}]
    fake.tables['macro_daily'] = [MACRO]
    fake.tables['app_config'] = [{'key': 'advisor_natwar_latest', 'value': json.dumps(NATWAR)}]
    with patch.object(mb.db, 'supabase', fake):
        out = mb.publish('2026-11-16')
    assert out['lines'][0]['text'].startswith('Kanaklata: 1 sell, 1 trim')      # lite row ZZZ ignored
    stored = [r for r in fake.tables['app_config'] if r['key'] == mb.KEY]
    assert stored and json.loads(stored[0]['value'])['run_date'] == '2026-11-16'


def test_brief_tax_line():
    rows = [{'symbol': 'ATGL', 'verdict': 'SELL', 'indicators': {'tax': {'action': 'sell after 2027-01-07', 'saving_inr': 4500, 'ltcg_date': '2027-01-07'}}},
            {'symbol': 'A', 'verdict': 'HOLD', 'indicators': {'tax': {'action': None, 'ltcg_date': '2026-12-01'}}},
            {'symbol': 'B', 'verdict': 'HOLD', 'indicators': {'tax': {'action': None, 'ltcg_date': '2026-12-08'}}},
            {'symbol': 'FAR', 'verdict': 'HOLD', 'indicators': {'tax': {'action': None, 'ltcg_date': '2027-06-01'}}}]
    lines = mb.compose('2026-11-16', rows, [], None, None, {}, None, None)
    by = {l['kind']: l['text'] for l in lines}
    assert by['tax'] == 'Tax: ATGL sell after 2027-01-07 (saves ₹4.5k) · 2 cross 12 months by Dec 8: A (12-01), B (12-08)'
    assert [l['kind'] for l in lines].index('tax') == 1
    assert 'tax' not in {l['kind'] for l in mb.compose('2026-11-16', PRIMARY, [], None, None, {}, None, None)}
