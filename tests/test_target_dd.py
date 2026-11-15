"""[P-57] Buy-target due diligence: flags with the number that triggered them."""
from unittest.mock import patch

import target_dd as td

CLEAN = {'pledge': 0.0, 'promoter': 50.0, 'promoter_change_3y': 0.0, 'fii_change': -0.85, 'interest_coverage': 22.5,
         'cfo': 4750.0, 'debt': 2115.0, 'cash': 11655.0, 'qtr_profit_growth': 51.3, 'qtr_sales_growth': -35.0,
         'pe': 10.6, 'up_from_low': 39.6, 'asof': '2026-11-11'}


def _names(flags):
    return [f['flag'] for f in flags]


def test_clean_record_flags_only_the_sales_dip():
    assert _names(td.flags_from_fundamentals(CLEAN)) == ['quarter_sales']


def test_every_rule_fires_with_its_number():
    bad = {**CLEAN, 'pledge': 12.0, 'promoter_change_3y': -7.5, 'promoter': 20.0, 'fii_change': -3.0,
           'interest_coverage': 1.2, 'cfo': -300.0, 'debt': 5000.0, 'cash': 100.0, 'qtr_profit_growth': -40.0,
           'pe': 80.0, 'up_from_low': 150.0}
    flags = td.flags_from_fundamentals(bad)
    by = {f['flag']: f for f in flags}
    assert by['pledge']['severity'] == 'red' and by['pledge']['detail'] == 'promoters pledged 12%'
    assert by['promoter_selling']['detail'] == 'promoter holding down 7.5pp in 3y'
    assert by['negative_cfo']['severity'] == 'red' and 'debt > 2× cash' in by['negative_cfo']['detail']
    assert by['chasing']['detail'] == '150% above its 52-week low'
    assert set(by) == {'pledge', 'promoter_selling', 'low_promoter', 'fii_exit', 'interest_coverage',
                       'negative_cfo', 'quarter_profit', 'quarter_sales', 'expensive', 'chasing'}
    # banks: leverage rules don't apply
    assert 'interest_coverage' not in _names(td.flags_from_fundamentals(bad, 'Financial Services'))
    assert _names(td.flags_from_fundamentals(None)) == ['no_fundamentals']


def test_news_and_attach():
    news = [{'symbols': ['PETRONET'], 'headline': 'Petronet faces penalty', 'sentiment_score': -0.5, 'published_at': '2026-11-10T09:00:00'},
            {'symbols': ['PETRONET'], 'headline': 'Good quarter', 'sentiment_score': 0.4, 'published_at': '2026-11-11T09:00:00'},
            {'symbols': ['OTHER'], 'headline': 'Fraud', 'sentiment_score': -0.9, 'published_at': '2026-11-11T09:00:00'}]
    assert td.flags_from_news('PETRONET', news) == [{'flag': 'negative_news', 'severity': 'red',
                                                     'detail': '2026-11-10: Petronet faces penalty'}]
    rows = [{'symbol': 'ATGL', 'rotation_target_symbol': 'PETRONET', 'indicators': {}},
            {'symbol': 'INFY', 'indicators': {}}]
    with patch.object(td, 'recent_news', return_value=news):
        n = td.attach(rows, {'asof': '2026-11-11', 'by_symbol': {'PETRONET': CLEAN}}, {'PETRONET': 'Oil Gas & Consumable Fuels'})
    assert n == 1
    dd = rows[0]['indicators']['target_dd']
    assert dd['symbol'] == 'PETRONET' and _names(dd['flags']) == ['quarter_sales', 'negative_news'] and dd['asof'] == '2026-11-11'
    assert 'target_dd' not in rows[1]['indicators']
