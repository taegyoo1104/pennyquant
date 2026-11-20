"""Reference tables must show the same ETF stream as the main dashboard."""
from copy import deepcopy
from datetime import timedelta

import pytest

from sessionquant.dashboard.home_benchmarks import benchmark_cards
from sessionquant.dashboard.reference_panel import reference_rows
from test_benchmark_trade_feed import NOW, ready, trade


@pytest.mark.parametrize('state', ['CONNECTED','BACKOFF','STOPPED'])
def test_soxx_price_matches_main_even_without_rest_price_record(state):
    feed = ready()
    for symbol, price in [('SPY','716.15'),('QQQ','645.39'),('SOXX','134.36')]:
        feed.accept(trade(symbol,price=price),feed.segment)
    stream = feed.snapshot()
    stream['state'] = state
    view = {'active_symbols':['SPY','QQQ','SOXX'],'items':{}}
    original = deepcopy((view,stream))
    rows = reference_rows(view,NOW,stream=stream)
    cards = benchmark_cards(view,NOW,stream=stream)
    assert {r['종목']:r['관측 가격'] for r in rows} == {c['symbol']:c['price'] for c in cards}
    soxx = next(r for r in rows if r['종목']=='SOXX')
    assert soxx['관측 가격']=='$134.36' and soxx['가격 종류']=='체결가'
    assert soxx['공급원']=='토스 웹소켓 체결'
    assert soxx['가격 수신']=={'CONNECTED':'최근 수신','BACKOFF':'재접속 중','STOPPED':'수신 중지'}[state]
    # Receiving a trade is not evidence that candle analytics are complete.
    assert soxx['품질']=='UNKNOWN' and soxx['분석 상태']=='확인 필요'
    assert soxx['5분 ETF 등락률(%)'] is None
    assert (view,stream)==original


def test_stale_saved_stream_is_not_presented_as_new_receipt():
    feed=ready()
    feed.accept(trade('SOXX',price='134.36'),feed.segment)
    view={'active_symbols':['SOXX'],'items':{}}
    row=reference_rows(view,NOW+timedelta(seconds=31),stream=feed.snapshot())[0]
    assert row['관측 가격']=='$134.36' and row['가격 수신']!='최근 수신'
    assert '가격 갱신 지연' in row['확인할 자료']
    invalid=feed.snapshot();invalid['updated_at']=(NOW+timedelta(seconds=10)).isoformat()
    assert reference_rows(view,NOW,stream=invalid)[0]['관측 가격']=='—'
