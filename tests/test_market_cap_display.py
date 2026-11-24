from copy import deepcopy

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.watchlist_panel import market_cap_text


@pytest.mark.parametrize('row,expected', [
    ({'currency': 'KRW', 'market_cap_value': '6624914389086336'}, '6,624조 9,143억 원'),
    ({'currency': 'KRW', 'market_cap_value': 4788404424993018}, '4,788조 4,044억 원'),
    ({'currency': 'USD', 'market_cap_usd': '3210456789012'}, '3조 2,104억 달러'),
    ({'market_cap_usd': '1000000000000'}, '1조 달러'),
    ({'currency': 'KRW', 'market_cap_value': '999999999999'}, '9,999억 원'),
    ({'currency': 'KRW', 'market_cap_value': '99999999'}, '9,999만 원'),
    ({'currency': 'KRW', 'market_cap_value': None, 'market_cap_usd': '10'}, '—'),
    ({'market_cap_usd': 'NaN'}, '—'), ({'market_cap_usd': '-1'}, '—'),
    ({'market_cap_usd': True}, '—'), ({'currency': 'EUR', 'market_cap_usd': '10'}, '—'),
])
def test_market_cap_preserves_currency_and_truncates_only_for_display(row, expected):
    original = deepcopy(row)
    assert market_cap_text(row) == expected
    assert row == original


def test_watchlist_table_and_csv_use_readable_units():
    state = {'items': [{'ticker': 'AAPL', 'currency': 'KRW', 'market_cap_value': '6624914389086336'}]}
    app = AppTest.from_string('from sessionquant.dashboard.watchlist_panel import render_watchlist\n'
                              f'render_watchlist({state!r}, [])').run()
    assert not app.exception
    table = app.dataframe[0].value
    assert table['시가총액'].iloc[0] == '6,624조 9,143억 원'
    assert '6,624조 9,143억 원' in table.to_csv(index=False)
