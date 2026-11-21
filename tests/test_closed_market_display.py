from datetime import datetime, timedelta, timezone

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.close_display import CloseDisplay, VIEW, completed_session, daily_close, day_key
from sessionquant.market_reference.trade_feed import BenchmarkTradeFeed
from sessionquant.dashboard.home_benchmarks import benchmark_cards, benchmark_cards_html
from sessionquant.dashboard.closed_market import closed_focus_cards

FRIDAY = datetime(2026, 11, 20, 21, tzinfo=timezone.utc)
SATURDAY = FRIDAY + timedelta(hours=15)


def daily(day, price):
    return {'timestamp':day+'T05:00:00Z', 'currency':'USD', 'closePrice':price,
            'openPrice':price, 'lowPrice':price, 'highPrice':price}


def response():
    return {'candles':[daily('2026-11-20','101'), daily('2026-11-19','100')]}


def snapshot():
    bounds = completed_session(SATURDAY)
    return {'session_date':day_key(bounds), 'captured_at':(FRIDAY-timedelta(minutes=1)).isoformat(),
        'focus':[{'ticker':'AMD', 'focused':True}],
        'items':{s:daily_close(response(),bounds,FRIDAY+timedelta(minutes=1)) for s in ('SPY','QQQ','SOXX','AMD')}}


def frame(at, price='101'):
    return {'topic':'trade:us:SPY','data':{'price':price,'currency':'USD','volume':'1','timestamp':at.isoformat()},
            'received_at':at.isoformat()}


def test_full_day_archive_survives_restart_weekend_and_resets_only_next_regular_session():
    now = [FRIDAY-timedelta(hours=6,minutes=30)]
    feed = BenchmarkTradeFeed(None,clock=lambda:now[0])
    feed.acknowledged=True
    for minute in range(391):
        now[0]=FRIDAY-timedelta(minutes=390-minute)
        feed.accept(frame(now[0]),feed.segment)
    now[0]=SATURDAY
    saved=feed.snapshot()
    assert saved['points']['SPY']==[]
    assert len(saved['session_points']['SPY'])==391
    restored=BenchmarkTradeFeed(None,clock=lambda:now[0],restored=saved)
    assert restored.snapshot()['session_points']==saved['session_points']
    now[0]=datetime(2026,11,23,13,tzinfo=timezone.utc)  # Monday preopen
    restored.acknowledged=True
    restored.accept(frame(now[0],'104'),restored.segment)
    assert len(restored.snapshot()['session_points']['SPY'])==391
    now[0]=now[0].replace(hour=14,minute=30)
    restored.accept(frame(now[0],'105'),restored.segment)
    assert len(restored.snapshot()['session_points']['SPY'])==1


def test_weekend_cards_use_close_not_afterhours_and_keep_chart_without_live_badge():
    at=FRIDAY-timedelta(minutes=1)
    stream={'updated_at':FRIDAY.isoformat(),'session_points':{'SPY':[
        [int((at-timedelta(minutes=1)).timestamp()*1000),'99',1], [int(at.timestamp()*1000),'100',1]]}}
    cards=benchmark_cards({'active_symbols':['SPY']},SATURDAY,stream=stream,closing=snapshot())
    card=cards[0]
    assert card['price']=='$101.00' and card['change']=='+1.00%'
    assert card['price_kind']=='정규장 종가' and not card['price_fresh']
    assert card['price_status']=='장 마감' and len(card['points'])==2
    assert '2026-11-20' in card['time_label']
    assert '<polyline' in benchmark_cards_html(cards)
    focus=closed_focus_cards(snapshot(),{}, {'AMD':{'symbol':'AMD','bid':'900','ask':'902',
        'timestamp':(FRIDAY+timedelta(hours=1)).isoformat(),'received_at':(FRIDAY+timedelta(hours=1)).isoformat()}},SATURDAY)
    assert focus[0]['price']=='$101.00' and focus[0]['change']=='+1.00%'
    assert focus[0]['steps']==[] and not focus[0]['monitored'] and not focus[0]['stage_fresh']


def test_missing_close_keeps_received_prices_and_neutral_chart_without_fabricating_return():
    at=FRIDAY-timedelta(minutes=1)
    stream={'updated_at':FRIDAY.isoformat(),'points':{'SPY':[
        [int((at-timedelta(minutes=1)).timestamp()*1000),'99',1], [int(at.timestamp()*1000),'100',1]]}}
    cards=benchmark_cards({'active_symbols':['SPY']},SATURDAY,stream=stream)
    assert cards[0]['price']=='$100.00' and cards[0]['change']=='전일 대비 미확인'
    assert '종가 미확인' in cards[0]['price_kind']
    html=benchmark_cards_html(cards)
    assert '<polyline' in html and '전일 종가 미확인 · 저장된 가격 흐름' in html
    # Future snapshots cannot leak prices into an older display.
    stream['updated_at']=(SATURDAY+timedelta(days=1)).isoformat()
    assert benchmark_cards({'active_symbols':['SPY']},SATURDAY,stream=stream)[0]['price']=='—'


@pytest.mark.parametrize('now,expected', [
    ('2026-11-21T12:00:00+00:00','2026-11-20'),
    ('2026-11-26T20:00:00+00:00','2026-11-25'), # Thanksgiving
    ('2026-11-27T18:01:00+00:00','2026-11-27'), # early close
    ('2026-03-09T20:01:00+00:00','2026-03-09'), # daylight saving
])
def test_completed_session_uses_exchange_calendar(now,expected):
    assert day_key(completed_session(datetime.fromisoformat(now)))==expected


def test_daily_close_rejects_forming_conflicting_and_wrong_currency():
    bounds=completed_session(SATURDAY)
    assert daily_close(response(),bounds,FRIDAY-timedelta(seconds=1)) is None
    data=response(); data['candles'].append(daily('2026-11-20','102'))
    assert daily_close(data,bounds,FRIDAY) is None
    data=response(); data['candles'][0]['currency']='KRW'
    assert daily_close(data,bounds,FRIDAY) is None


@pytest.mark.asyncio
async def test_close_collector_freezes_focus_and_uses_two_optional_calls(tmp_path):
    repo=Repository(tmp_path/'closing.db',backend='sqlite')
    monitor=CloseDisplay(repo)
    monitor.capture({'items':[{'ticker':'AMD','focused':True}]},FRIDAY-timedelta(minutes=1))
    monitor.capture({'items':[]},SATURDAY)
    calls=[]
    async def read(symbol):
        calls.append(symbol)
        return {'data':response(),'received_at':(FRIDAY+timedelta(minutes=1)).isoformat()}
    await monitor.collect(read,FRIDAY+timedelta(minutes=1))
    assert calls==['SPY','QQQ']
    await monitor.collect(read,FRIDAY+timedelta(minutes=2))
    assert calls==['SPY','QQQ','SOXX','AMD']
    await monitor.collect(read,FRIDAY+timedelta(minutes=3))
    assert len(calls)==4
    state=repo.get_control(VIEW)
    assert state['focus'][0]['ticker']=='AMD' and state['items']['AMD']['price']=='101'
    assert not repo.recent('orders') and not repo.recent('executions')
    repo.close()


def test_overnight_close_view_switches_to_premarket_at_four_new_york():
    from sessionquant.market.close_display import closed_market
    assert closed_market(datetime(2026,11,23,7,tzinfo=timezone.utc))  # 02:00 NY
    assert not closed_market(datetime(2026,11,23,9,tzinfo=timezone.utc))  # 04:00 NY
    assert day_key(completed_session(datetime(2026,11,23,7,tzinfo=timezone.utc)))=='2026-11-20'


def test_old_or_future_focus_is_not_relabelled_as_latest_trading_day():
    state={'updated_at':'2026-11-19T20:59:00Z','items':[{'ticker':'AMD','focused':True}]}
    assert closed_focus_cards({},state,{},SATURDAY)==[]
    saved=snapshot();saved['captured_at']=(SATURDAY+timedelta(days=1)).isoformat()
    assert closed_focus_cards(saved,{}, {},SATURDAY)==[]


@pytest.mark.asyncio
async def test_failed_closing_requests_stop_after_three_attempts(tmp_path):
    repo=Repository(tmp_path/'closing-failure.db',backend='sqlite')
    monitor=CloseDisplay(repo)
    calls=[]
    now=[FRIDAY+timedelta(seconds=5)]
    async def read(symbol):
        calls.append(symbol)
        return {'data':None,'received_at':now[0].isoformat()}
    for _ in range(10):
        await monitor.collect(read,now[0])
        now[0]+=timedelta(minutes=1)
    assert all(calls.count(s)==3 for s in ('SPY','QQQ','SOXX'))
    assert not monitor.state['items']
    repo.close()
