"""Reference-only prices, daily colors and charts must preserve missing evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from xml.etree import ElementTree

import pytest

from sessionquant.dashboard.home_benchmarks import benchmark_cards, benchmark_cards_html, _chart
from sessionquant.database.repository import Repository
from sessionquant.market_reference.collector import MarketReferenceMonitor
from sessionquant.market_reference.contracts import observation
from sessionquant.market_reference.store import ReferenceStore

NOW = datetime(2026,11,19,15,30,tzinfo=timezone.utc)
PREVIOUS = datetime(2026,11,18,21,tzinfo=timezone.utc)


def reference_view(tmp_path, *, previous='100', price='99'):
    repo=Repository(tmp_path/'reference.db',backend='sqlite')
    store=ReferenceStore(repo)
    for symbol in ('SPY','QQQ'):
        store.ingest(observation(symbol,'daily',{'open':previous,'high':previous,'low':previous,
            'close':previous,'volume':'100','currency':'USD'},PREVIOUS,NOW,NOW))
        for minutes,value in ((2,'98'),(1,'98.5'),(0,'99')):
            at=NOW-timedelta(minutes=minutes)
            store.ingest(observation(symbol,'candle',{'open':value,'high':value,'low':value,
                'close':value,'volume':'10','currency':'USD'},at,NOW,NOW))
        store.ingest(observation(symbol,'price',{'price':price,'currency':'USD'},NOW,NOW,NOW))
    view=store.publish(NOW,symbols=('SPY','QQQ'))
    assert not repo.all('orders') and not repo.all('executions')
    repo.close()
    return view


@pytest.mark.parametrize('previous,price,tone,change',[
    ('100','99','down','-1.00%'),
    ('98','99','up','+1.02%'),
    ('99','99','flat','+0.00%'),
])
def test_daily_change_uses_previous_close_not_the_intraday_line_slope(tmp_path,previous,price,tone,change):
    view=reference_view(tmp_path,previous=previous,price=price)
    original=deepcopy(view)
    cards=benchmark_cards(view,NOW)
    assert all(c['tone']==tone and c['change']==change for c in cards)
    # The minute line rises even when the price is below yesterday's close.
    assert cards[0]['points'][0][1] < cards[0]['points'][-1][1]
    assert view==original


@pytest.mark.parametrize('field,value',[
    ('previous_close',None),('previous_close','NaN'),('previous_close','0'),
    ('previous_close_at',None),('previous_close_at','2026-11-17T21:00:00Z'),
    ('previous_close_at','2026-11-19T21:00:00Z'),
])
def test_missing_invalid_or_wrong_day_close_does_not_invent_daily_return(tmp_path,field,value):
    view=reference_view(tmp_path);view['items']['SPY'][field]=value
    card=benchmark_cards(view,NOW)[0]
    assert card['tone']=='flat' and card['change']=='전일 대비 확인 중'
    assert '<svg' not in benchmark_cards_html([card])


@pytest.mark.parametrize('values,colors',[
    (['99','99.5'],['#60a5fa']), (['101','100.5'],['#f87171']),
    (['99','101'],['#60a5fa','#f87171']), (['101','99'],['#f87171','#60a5fa']),
    (['100','100'],['#94a3b8']),
])
def test_center_is_previous_close_and_crossings_switch_color_exactly_at_zero(values,colors):
    points=[(NOW+timedelta(minutes=i),Decimal(v)) for i,v in enumerate(values)]
    root=ElementTree.fromstring('<div>'+_chart(points,Decimal('100'),'SPY')+'</div>')
    lines=root.findall('.//polyline')
    assert [line.attrib['stroke'] for line in lines]==colors
    for line in lines:
        ys=[float(p.split(',')[1]) for p in line.attrib['points'].split()]
        color=line.attrib['stroke']
        if color=='#60a5fa':assert min(ys)>=64
        elif color=='#f87171':assert max(ys)<=64
        else:assert all(y==64 for y in ys)
    if len(colors)==2:
        assert lines[0].attrib['points'].split()[-1]=='150.00,64.00'
        assert lines[1].attrib['points'].split()[0]=='150.00,64.00'
    elif values[0]!='100':
        assert float(lines[0].attrib['points'].split()[0].split(',')[1])!=64


def test_gaps_future_invalid_and_conflicting_points_do_not_form_a_fake_continuous_line(tmp_path):
    view=reference_view(tmp_path)
    rows=view['items']['SPY']['trend_points']
    rows.pop(1)
    rows += [{'at':(NOW+timedelta(minutes=1)).isoformat(),'available_at':NOW.isoformat(),'price':'500'},
             {'at':NOW.isoformat(),'available_at':NOW.isoformat(),'price':'NaN'}]
    cards=benchmark_cards(view,NOW)
    html=benchmark_cards_html(cards[:1])
    assert len(cards[0]['points'])==2 and '<polyline' not in html and html.count('<circle')==2
    assert '분봉 일부 없음' in html
    rows.append({**rows[0],'price':'77'})
    assert len(benchmark_cards(view,NOW)[0]['points'])==1


def test_old_and_empty_snapshots_are_never_labelled_as_fresh(tmp_path):
    view=reference_view(tmp_path)
    old=benchmark_cards(view,NOW+timedelta(minutes=6))[0]
    assert old['price_status']=='이전 가격' and old['trend_status']=='이전 분봉 기록'
    assert not old['price_fresh']
    missing=benchmark_cards({},NOW)
    assert all(c['price']=='—' and c['points']==[] and c['tone']=='flat' for c in missing)
    assert '분봉을 받으면' in benchmark_cards_html(missing)


def test_weekend_comparison_uses_last_trading_day(tmp_path):
    view=reference_view(tmp_path)
    monday=datetime(2026,11,23,15,30,tzinfo=timezone.utc)
    item=view['items']['SPY'];item['previous_close_at']='2026-11-20T21:00:00Z'
    price=item['latest_observations']['price']
    for field in ('event_at','received_at','available_at'):price[field]=monday.isoformat()
    assert benchmark_cards(view,monday)[0]['change']=='-1.00%'


async def test_display_collector_queries_only_selected_etfs_and_price_candle_daily_streams(tmp_path):
    repo=Repository(tmp_path/'scope.db',backend='sqlite');calls=[]
    monitor=MarketReferenceMonitor(repo,clock=lambda:NOW,symbols=['SPY','QQQ'],display_only=True,calls_per_cycle=2)
    async def read(stream,symbol):
        calls.append((stream,symbol))
        return {'data':None,'error':'fixture_missing'}
    for _ in range(3):
        before=len(calls)
        view=await monitor.poll(read)
        assert len(calls)-before<=2
    assert set(calls)=={('prices','*'),('candle','SPY'),('daily','SPY'),('candle','QQQ'),('daily','QQQ')}
    assert set(view['items'])=={'SPY','QQQ'}
    assert not repo.all('orders') and not repo.all('executions')
    repo.close()


async def test_failed_daily_read_retries_within_budget_then_refreshes_at_next_open(tmp_path):
    repo=Repository(tmp_path/'retry.db',backend='sqlite');calls=[];clock=[NOW]
    monitor=MarketReferenceMonitor(repo,clock=lambda:clock[0],symbols=['SPY'],display_only=True,calls_per_cycle=3)
    async def read(stream,symbol):
        calls.append((stream,symbol))
        if stream!='daily' or len([c for c in calls if c[0]=='daily'])==1:
            return {'data':None,'error':'temporary_failure'}
        return {'data':{'candles':[{'openPrice':'100','highPrice':'100','lowPrice':'100',
            'closePrice':'100','volume':'1000','currency':'USD','timestamp':PREVIOUS.isoformat()}]}}
    await monitor.poll(read)
    clock[0]+=timedelta(seconds=299);await monitor.poll(read)
    assert len([c for c in calls if c[0]=='daily'])==1
    clock[0]+=timedelta(seconds=1);await monitor.poll(read)
    assert len([c for c in calls if c[0]=='daily'])==2
    clock[0]+=timedelta(minutes=10);await monitor.poll(read)
    assert len([c for c in calls if c[0]=='daily'])==2
    clock[0]=datetime(2026,11,20,14,30,tzinfo=timezone.utc);await monitor.poll(read)
    assert len([c for c in calls if c[0]=='daily'])==3
    repo.close()


@pytest.mark.parametrize('value',[{'symbols':[]},{'symbols':['AAPL']},{'symbols':['SPY','SPY']},
    {'symbols':'SPY'},{'display_only':'true'}])
def test_invalid_reference_scope_is_rejected(value):
    from sessionquant.market_reference.settings import reference_settings
    with pytest.raises(ValueError):reference_settings(value)


async def test_soxx_is_optional_display_only_and_preserves_legacy_risk_scope(tmp_path):
    from sessionquant.market_reference.settings import reference_settings
    from sessionquant.market_reference.risk import MacroReferenceRiskMonitor
    from sessionquant.market_reference.contracts import REFERENCE_SYMBOLS
    from sessionquant.session_config import load_session_config
    repo = Repository(tmp_path/'semiconductors.db',backend='sqlite')
    calls = []
    symbols = ['SPY','QQQ','SOXX']
    for profile in ('strategy-lab','strategy-lab-websocket'):
        config = load_session_config('config/sessionquant.'+profile+'.example.yaml')
        assert config.market_reference['symbols'] == symbols
        assert not config.market_filter_enabled
    assert 'SOXX' in REFERENCE_SYMBOLS  # Never a stock trading candidate.
    assert 'SOXX' not in reference_settings({})['symbols']
    monitor = MarketReferenceMonitor(repo,clock=lambda:NOW,symbols=symbols,display_only=True,calls_per_cycle=2)
    async def read(stream,symbol):
        calls.append((stream,symbol))
        return {'data':None,'error':'fixture_missing'}
    for _ in range(4):
        before = len(calls)
        view = await monitor.poll(read)
        assert len(calls)-before <= 2
    assert ('daily','SOXX') in calls and ('candle','SOXX') in calls
    cards = benchmark_cards(view,NOW)
    assert [c['symbol'] for c in cards] == symbols
    assert cards[-1]['name'] == '미국 반도체 ETF' and cards[-1]['price'] == '—'
    assert benchmark_cards_html(cards).count('<article ') == 3
    assert len(benchmark_cards(view,NOW,symbols=['SPY','QQQ'])) == 2
    measurements,_ = MacroReferenceRiskMonitor(repo)._measure(view,NOW)
    assert 'SOXX' not in measurements
    assert not repo.all('orders') and not repo.all('executions')
    repo.close()
