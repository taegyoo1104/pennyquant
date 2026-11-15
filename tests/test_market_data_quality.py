"""단계 02: 모의 입력과 격리 원장에서 실제 입력과 UNKNOWN의 경계를 검사한다."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.collectors.common import DataQualityError
from sessionquant.collectors.toss_market import TossMarketClient, parse_candles, parse_orderbook
from sessionquant.database.repository import Repository
from sessionquant.market.bars import aggregate_bars, calendar, session_bounds
from sessionquant.market.eligibility import toss_eligibility
from sessionquant.market.engine import MarketEngine
from sessionquant.market.quality import QUALITY_KEY, bar_quality, rvol_report
from sessionquant.market.reporting import current_report, persist_quality
from sessionquant.market.scanner import InstrumentEligibility
from sessionquant.market.universe import UniverseStore
from sessionquant.models import Bar, Quote
from sessionquant.scanners.relative_volume import relative_volume
from sessionquant.session_engine import SessionEngine
from sessionquant.watchlist.selection import WatchlistManager
from test_watchlist_source import Market
from watchlist_fixtures import NOW as WATCH_NOW, context, observe

NOW = datetime(2026,11,13,14,35,2,tzinfo=timezone.utc)
THROUGH = NOW.replace(second=0)
SOURCE = 'SYNTHETIC_VERIFIED_PROVIDER'


def minutes(symbol='TEST', through=THROUGH, *, volume=2000):
    opened,_ = session_bounds(through)
    return [Bar(symbol,opened+timedelta(minutes=i),NOW,100,101,99,100,volume,
                source=SOURCE,price_adjustment='raw') for i in range(1,int((through-opened).total_seconds()//60)+1)]


def full_history():
    cal = calendar(2026)
    sessions = cal.sessions_in_range('2026-10-01','2026-11-12')[-20:]
    rows = []
    for day in sessions:
        opened = cal.session_open(day).to_pydatetime()
        for minute in range(1,6):
            rows.append(Bar('TEST',opened+timedelta(minutes=minute),NOW,100,101,99,100,1000,
                            source=SOURCE,price_adjustment='raw'))
    return rows+minutes()


def volume_evidence(rows):
    return {'status':'VERIFIED','source':SOURCE,'split_evidence_source':'SYNTHETIC_SPLIT_FIXTURE',
        'received_at':NOW.isoformat(),'valid_until':(NOW+timedelta(minutes=5)).isoformat(),
        'range_start':(min(b.timestamp for b in rows)-timedelta(minutes=1)).isoformat(),
        'range_end':THROUGH.isoformat(),'price_adjustment':'raw','volume_policy':'no_splits_verified'}


def stock(**extra):
    return {'symbol':'TEST','currency':'USD','market':'NASDAQ','status':'ACTIVE',**extra}


@pytest.mark.parametrize('extra',[{}, {'tradable':True,'fractional':True,'halted':False,'corporate_action':False},
                                  {'koreanMarketDetail':{'krxTradingSuspended':False}}])
def test_stock_active_or_injected_permissions_never_prove_us_account_eligibility(extra):
    item = toss_eligibility('TEST',stock(**extra),NOW)
    assert item.tradable is None and item.fractional is None and item.halted is None and item.corporate_action is None
    assert item.exchange == 'NASDAQ' and item.account_scope == 'UNKNOWN'
    assert item.checked_at == NOW and item.valid_until == NOW+timedelta(days=1)
    assert all(item.field_evidence[key]['status']=='UNKNOWN' for key in ['tradable','fractional','halted','corporate_action'])


@pytest.mark.parametrize('value',['DELISTED','SCHEDULED'])
def test_listing_restriction_can_reject_but_never_grant_permissions(value):
    item = toss_eligibility('TEST',stock(status=value),NOW)
    assert item.tradable is False
    assert item.field_evidence['listing_status']['value'] == value
    assert item.field_evidence['listing_status']['source'] == 'toss:StockInfo.status'


def test_ticker_replacement_and_currency_mismatch_do_not_reuse_exchange():
    assert toss_eligibility('OTHER',stock(),NOW).exchange == 'UNKNOWN'
    assert toss_eligibility('TEST',stock(currency='KRW'),NOW).exchange == 'UNKNOWN'


def test_expired_future_wrong_account_or_missing_evidence_blocks_strict_engine():
    from sessionquant.market.eligibility import field_verified
    from test_session_market import engine, NOW as at
    instance = engine()
    instance.strict_data = True
    original = instance.eligibility['AAPL']
    assert 'eligibility_unverified' in instance.evaluate('AAPL',at).blockers
    row = {'status':'VERIFIED','value':True,'source':'SYNTHETIC_ACCOUNT_FIXTURE','account_scope':'FIXTURE_A',
           'as_of_at':at.isoformat(),'received_at':at.isoformat(),'valid_until':(at+timedelta(seconds=10)).isoformat()}
    item = replace(original,account_scope='FIXTURE_A',field_evidence={'tradable':row},valid_until=at+timedelta(seconds=10))
    assert field_verified(item,'tradable',True,at)
    assert not field_verified(replace(item,account_scope='FIXTURE_B'),'tradable',True,at)
    assert not field_verified(item,'tradable',True,at+timedelta(seconds=11))
    assert not field_verified(item,'tradable',True,at-timedelta(seconds=1))
    assert instance.evaluate('AAPL',at).signal is None


def test_missing_zero_minutes_are_not_filled_and_conflicting_revisions_are_unknown():
    rows = minutes()
    explicit_zero = [replace(b,volume=0) if i==2 else b for i,b in enumerate(rows)]
    quality = bar_quality(explicit_zero,'TEST',NOW)
    assert quality['status']=='COMPLETE' and quality['explicit_zero_volume_minutes']==1
    missing = bar_quality(explicit_zero[:2]+explicit_zero[3:],'TEST',NOW)
    assert missing['missing_minutes']==1 and missing['missing_rate']==.2
    assert missing['five_count']==0 and len(aggregate_bars(explicit_zero[:2]+explicit_zero[3:],5,NOW))==0
    conflict = bar_quality(rows+[replace(rows[0],volume=1)],'TEST',NOW)
    assert conflict['conflicting_minutes']==1 and conflict['five_count']==0
    duplicate = bar_quality(rows+[rows[0]],'TEST',NOW)
    assert duplicate['identical_duplicates']==1 and duplicate['status']=='COMPLETE'


def test_delayed_correction_only_affects_quality_after_receipt():
    rows = minutes()
    corrected = replace(rows[0],volume=1,received_at=NOW+timedelta(seconds=1))
    assert bar_quality(rows+[corrected],'TEST',NOW)['status']=='COMPLETE'
    assert bar_quality(rows+[corrected],'TEST',NOW+timedelta(seconds=1))['status']=='INCOMPLETE'
    assert bar_quality(rows,'TEST',NOW+timedelta(minutes=2))['age_seconds']==122


@pytest.mark.parametrize('now,expected,opened',[
    ('2026-11-26T15:00:00+00:00',0,None),
    ('2026-11-27T18:01:00+00:00',210,'2026-11-27T14:30:00+00:00'),
    ('2026-03-09T13:35:00+00:00',5,'2026-03-09T13:30:00+00:00'),
    ('2026-03-06T14:35:00+00:00',5,'2026-03-06T14:30:00+00:00')])
def test_quality_calendar_holidays_early_close_and_dst(now,expected,opened):
    report = bar_quality([],'TEST',datetime.fromisoformat(now))
    assert report['expected_minutes']==expected
    if opened: assert report['session_open']==opened
    else: assert report['status']=='CLOSED' and report['missing_rate'] is None


def test_rvol_twenty_same_elapsed_sessions_use_actual_knowledge_time():
    rows = full_history()
    proof = volume_evidence(rows)
    report = rvol_report(rows,'TEST',THROUGH,NOW,evidence=proof)
    assert report['status']=='VERIFIED' and report['value']==2 and report['complete_sessions']==20
    assert relative_volume(10000,THROUGH,rows,symbol='TEST',corporate_actions_verified=True) is None
    assert relative_volume(10000,THROUGH,rows,symbol='TEST',corporate_actions_verified=True,observed_at=NOW)==2
    assert rvol_report(rows,'TEST',THROUGH,NOW-timedelta(seconds=3),evidence=proof)['value'] is None


@pytest.mark.parametrize('case',['missing_history','missing_current','correction','no_proof','expired_proof',
                                  'future_proof','mixed_source','mixed_adjustment','split_unknown'])
def test_rvol_reports_exact_missing_inputs_and_cannot_infer_split_comparability(case):
    rows = full_history(); proof = volume_evidence(rows)
    if case=='missing_history':rows.pop(0)
    if case=='missing_current':rows.pop()
    if case=='correction':rows.append(replace(rows[0],volume=500))
    if case=='no_proof':proof={}
    if case=='expired_proof':proof['valid_until']=(NOW-timedelta(seconds=1)).isoformat()
    if case=='future_proof':proof['received_at']=(NOW+timedelta(seconds=1)).isoformat()
    if case=='mixed_source':rows[0]=replace(rows[0],source='OTHER_PROVIDER')
    if case=='mixed_adjustment':rows[0]=replace(rows[0],price_adjustment='adjusted')
    if case=='split_unknown':proof['volume_policy']='UNKNOWN'
    report = rvol_report(rows,'TEST',THROUGH,NOW,evidence=proof)
    assert report['value'] is None and report['reasons']
    if case in {'missing_history','correction'}: assert report['complete_sessions']==19


def test_split_adjusted_volumes_require_explicit_evidence_not_price_flag_alone():
    rows = [replace(b,price_adjustment='adjusted') for b in full_history()]
    proof = {**volume_evidence(rows),'price_adjustment':'adjusted','volume_policy':'split_adjusted_verified'}
    assert rvol_report(rows,'TEST',THROUGH,NOW,evidence=proof)['value']==2
    proof.pop('split_evidence_source')
    assert rvol_report(rows,'TEST',THROUGH,NOW,evidence=proof)['value'] is None


def test_strict_rvol_ignores_external_scalar_and_boolean_history_attestation():
    universe = UniverseStore();universe.refresh(['TEST'],source='SYNTHETIC_FIXTURE',observed_at=NOW-timedelta(days=1))
    engine = MarketEngine(universe,strict_data=True)
    for bar in full_history():engine.on_bar(bar)
    engine.set_rvol('TEST',999,NOW,verified=True)
    engine.set_eligibility('TEST',InstrumentEligibility('TEST',NOW,'SYNTHETIC_FIXTURE',True,True,False,False,'NASDAQ',True))
    result = engine.evaluate('TEST',NOW)
    assert result.indicators['relative_volume'] is None
    assert 'split_volume_comparability_unknown' in result.indicators['data_quality']['rvol']['reasons']
    assert not result.eligible
    # 적격성 불리언과 분리된, 범위·출처가 확인된 거래량 근거는 계산에만 사용한다.
    engine.eligibility['TEST'] = replace(engine.eligibility['TEST'],volume_evidence=volume_evidence(full_history()))
    verified = engine.evaluate('TEST',NOW)
    assert verified.indicators['relative_volume']==2 and not verified.eligible


@pytest.mark.parametrize('change',[{'volume':'1.5'},{'volume':'NaN'},{'timestamp':'2026-11-13T14:35:01Z'},
                                   {'currency':'KRW'},{'volume':True},{'lowPrice':'101'}])
def test_candle_parser_rejects_lossy_volume_or_invalid_boundaries_without_echo(change):
    row = {'timestamp':THROUGH.isoformat(),'currency':'USD','openPrice':'100','highPrice':'101',
           'lowPrice':'99','closePrice':'100','volume':'1000',**change}
    with pytest.raises(DataQualityError):parse_candles('TEST',{'candles':[row]},NOW)


def test_parser_provenance_and_incomplete_bar_measurement():
    row = {'timestamp':THROUGH.isoformat(),'currency':'USD','openPrice':'100','highPrice':'101',
           'lowPrice':'99','closePrice':'100','volume':'0'}
    report = {}
    bars = parse_candles('TEST',{'candles':[row,{**row,'timestamp':(NOW+timedelta(minutes=1)).replace(second=0).isoformat()}]},NOW,observation=report)
    assert len(bars)==1 and bars[0].volume==0
    assert bars[0].price_adjustment=='raw' and bars[0].source=='toss:/api/v1/candles:1m'
    assert report['future_excluded']==1 and report['volume_comparability']=='UNKNOWN'


def test_inverted_or_future_orderbook_is_rejected_and_stale_quote_not_relabelled():
    book = {'currency':'USD','timestamp':NOW.isoformat(),'asks':[{'price':'99','volume':'1'}],
            'bids':[{'price':'100','volume':'1'}]}
    with pytest.raises(DataQualityError):parse_orderbook('TEST',book,NOW)
    with pytest.raises(DataQualityError):parse_orderbook('TEST',{**book,'timestamp':(NOW+timedelta(seconds=1)).isoformat()},NOW)


async def test_mock_http_official_stock_fields_rate_headers_and_no_order_methods(monkeypatch):
    monkeypatch.setattr('sessionquant.collectors.toss_market.now_utc',lambda:NOW)
    calls=[]
    def handler(request):
        calls.append((request.method,request.url.path))
        assert request.method=='GET'
        return httpx.Response(200,json={'result':[stock(fractional=True)]},headers={
            'X-RateLimit-Remaining':'5','Authorization':'PRIVATE','X-Secret':'PRIVATE'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TossMarketClient('dummy','dummy',client=http);client._token='dummy';client._token_deadline=float('inf');client.limiter.interval=0
        values = await client.stocks(['TEST'])
        item = toss_eligibility('TEST',values[0],NOW)
        assert item.fractional is None
        telemetry = client.read_observations['/api/v1/stocks']
        assert telemetry['rate_headers']=={'x-ratelimit-remaining':'5'} and telemetry['cost_usd'] is None
        assert 'PRIVATE' not in str(telemetry)
    assert calls==[('GET','/api/v1/stocks')]


async def test_priority_quotes_delivered_before_slow_discovery_and_history_budget_is_bounded(tmp_path):
    repo,_,_,_,manager = context(tmp_path)
    try:
        await manager.refresh(WATCH_NOW);observe(manager)
        transitions=[];delivered=[]
        class Source(Market):
            async def prices(self,symbols):
                transitions.append('discovery')
                assert delivered and 'T001' in delivered[0]
                raise TimeoutError('PRIVATE')
            async def candles(self,symbol,**kwargs):
                bars,_ = await super().candles(symbol,**kwargs)
                return bars,(WATCH_NOW-timedelta(days=1)).isoformat()
        def receive(batch):
            if batch.quotes:
                delivered.append([q.symbol for q in batch.quotes]);transitions.append('priority')
        source = __import__('sessionquant.market.source',fromlist=['TossSessionSource']).TossSessionSource(
            Source(lambda:WATCH_NOW+timedelta(seconds=60)),clock=lambda:WATCH_NOW+timedelta(seconds=60),
            watchlist=manager,on_priority_batch=receive,history_pages_per_cycle=1)
        batch = await source.poll([],priority_symbols=['T001'])
        assert transitions[:2]==['priority','discovery']
        assert all(q.symbol!='T001' for q in batch.quotes)
        assert 'PRIVATE' not in str(batch.errors)
        assert sum(r['source']=='toss_history_backfill' and r['read_attempts']>0 for r in manager.state['read_measurements'])<=1
        assert manager.state['history_progress'] and manager.state['last_cycle_calls']<=24
        restored = WatchlistManager(repo,manager.universe,manager.config,allow_synthetic=True)
        assert restored.state['history_progress']==manager.state['history_progress']
    finally:repo.close()


def test_quality_persistence_restoration_coverage_and_dashboard_staleness(tmp_path,monkeypatch):
    universe = UniverseStore();universe.refresh(['TEST'],source='SYNTHETIC_FIXTURE',observed_at=NOW-timedelta(days=1))
    repo = Repository(tmp_path/'quality.db',backend='sqlite')
    try:
        engine = SessionEngine(repo,universe,synthetic=True,strict_market_data=True)
        for b in minutes():engine.market.on_bar(b)
        engine.market.on_quote(Quote('TEST',THROUGH,NOW,100,100.01,10,10,False,False,'NASDAQ'))
        engine.market.evaluate('TEST',NOW)
        persist_quality(engine,NOW)
        report = repo.get_control(QUALITY_KEY)
        assert report['required_data_coverage']['eligibility']['verified']==0
        assert report['required_data_coverage']['minute_history']['verified']==1
        assert report['required_data_coverage']['rvol']['verified']==0
        stale = current_report(report,NOW+timedelta(seconds=31))
        assert all(m['verified']==0 for m in stale['required_data_coverage'].values())
        assert stale['blocker_counts']['quality_observation_stale']==1
        assert repo.all('orders')==[] and report['synthetic']
    finally:repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite')
    monkeypatch.setattr('sys.argv',['session_app.py','--db',str(tmp_path/'quality.db')])
    app = AppTest.from_file(str(Path(__file__).parents[1]/'sessionquant/dashboard/session_app.py'),default_timeout=30).run()
    assert not app.exception
    app.radio(key='dashboard_page').set_value('시장·종목').run()
    assert any(m.label=='RVOL 근거' and m.value=='0 / 1' for m in app.metric)
    assert any('합성 입력의 데이터 품질' in w.value for w in app.warning)
    assert any('quality_observation_stale' in str(frame.value) for frame in app.dataframe)


def test_old_bar_identifiers_and_eligibility_restore_accept_optional_metadata(tmp_path):
    engine = MarketEngine(UniverseStore())
    bar = minutes()[0];engine.on_bar(bar)
    evidence = toss_eligibility('TEST',stock(),NOW)
    engine.set_eligibility('TEST',evidence)
    payload = json.loads(json.dumps(engine.to_state()))
    restored = MarketEngine(UniverseStore());restored.restore(payload)
    assert restored.eligibility['TEST']==evidence and restored.bars['TEST'][0].source==SOURCE
    old = {k:v for k,v in payload['bars']['TEST'][0].items() if k not in {'source','price_adjustment'}}
    payload['bars']['TEST']=[old]
    restored.restore(payload)
    assert restored.bars['TEST'][0].source=='UNKNOWN'


@pytest.mark.parametrize('cursor',[123,'PRIVATE','2027-01-01T00:00:00Z'])
async def test_mock_http_invalid_history_cursor_fails_closed_without_echo(cursor,monkeypatch):
    monkeypatch.setattr('sessionquant.collectors.toss_market.now_utc',lambda:NOW)
    def handler(request):
        assert request.url.params['adjusted']=='false'
        return httpx.Response(200,json={'result':{'candles':[],'nextBefore':cursor}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TossMarketClient('dummy','dummy',client=http);client._token='dummy';client._token_deadline=float('inf');client.limiter.interval=0
        with pytest.raises(DataQualityError) as exc:
            await client.candles('TEST')
        assert 'PRIVATE' not in str(exc.value)
        assert client.candle_observations['TEST']['status']=='INVALID_CURSOR'


async def test_stock_api_403_does_not_grant_eligibility_or_stop_priority_quotes(tmp_path,monkeypatch):
    stamp = WATCH_NOW+timedelta(seconds=60)
    monkeypatch.setattr('sessionquant.collectors.toss_market.now_utc',lambda:stamp)
    repo,_,_,_,manager = context(tmp_path)
    requests=[]
    def handler(request):
        requests.append(request.url.path)
        assert request.method=='GET'
        if request.url.path=='/api/v1/stocks':return httpx.Response(403,json={'error':{'message':'PRIVATE'}})
        if request.url.path=='/api/v1/orderbook':
            return httpx.Response(200,json={'result':{'currency':'USD','timestamp':stamp.isoformat(),
                'asks':[{'price':'100.01','volume':'100'}],'bids':[{'price':'100','volume':'100'}]}})
        if request.url.path=='/api/v1/candles':return httpx.Response(200,json={'result':{'candles':[],'nextBefore':None}})
        if request.url.path=='/api/v1/prices':return httpx.Response(200,json={'result':[]})
        return httpx.Response(200,json={'result':{'rankings':[],'rankedAt':None}})
    try:
        await manager.refresh(WATCH_NOW);observe(manager)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TossMarketClient('dummy','dummy',client=http);client._token='dummy';client._token_deadline=float('inf');client.limiter.interval=0
            from sessionquant.market.source import TossSessionSource
            source = TossSessionSource(client,watchlist=manager,clock=lambda:stamp)
            batch = await source.poll([],priority_symbols=['OUTSIDE'])
        assert {'OUTSIDE','SPY','QQQ'} <= {q.symbol for q in batch.quotes}
        assert batch.eligibility[0].symbol=='OUTSIDE' and batch.eligibility[0].fractional is None
        assert any(e.get('http_status')==403 for e in batch.errors) and 'PRIVATE' not in str(batch.errors)
        assert manager.state['last_cycle_calls']==len(requests)
    finally:repo.close()


def test_quality_summary_keeps_unqueried_focus_in_coverage_denominator(tmp_path):
    universe = UniverseStore();universe.refresh(['TEST','OTHER'],source='SYNTHETIC_FIXTURE',observed_at=NOW-timedelta(days=1))
    repo = Repository(tmp_path/'pending.db',backend='sqlite')
    try:
        from types import SimpleNamespace
        scope = SimpleNamespace(focus=lambda now:['TEST','OTHER'],state={})
        engine = SessionEngine(repo,universe,synthetic=True,strict_market_data=True,watchlist_policy=scope)
        persist_quality(engine,NOW)
        report = repo.get_control(QUALITY_KEY)
        assert report['evaluated_symbols']==2
        assert report['required_data_coverage']['quote']=={'verified':0,'required':2,'ratio':0}
        assert report['blocker_counts']['symbol_not_yet_evaluated']==2
    finally:repo.close()


async def test_history_backfill_repairs_restart_gap_even_when_open_minute_exists(tmp_path):
    from sessionquant.market.source import TossSessionSource
    repo,_,_,_,manager = context(tmp_path)
    try:
        await manager.refresh(WATCH_NOW);observe(manager)
        stamp = WATCH_NOW+timedelta(seconds=60)
        opened,_ = session_bounds(stamp)
        recent = stamp-timedelta(minutes=10)
        old_cursor = (WATCH_NOW-timedelta(days=2)).isoformat()
        manager.state['history_progress']={'T001':{'started':True,'done':True,'cursor':old_cursor,'status':'EXHAUSTED','pages':4}}
        class Recent(Market):
            async def candles(self,symbol,**kwargs):
                self.calls.append(('candles',symbol))
                return [Bar(symbol,recent,stamp,100,101,99,100,10)],recent.isoformat()
        source = TossSessionSource(Recent(lambda:stamp),clock=lambda:stamp,watchlist=manager,history_pages_per_cycle=0)
        source.restore_seen({'T001':[Bar('T001',opened+timedelta(minutes=1),opened+timedelta(minutes=1),100,101,99,100,10)]})
        await source.poll([],priority_symbols=['T001'])
        progress = manager.state['history_progress']['T001']
        assert progress['cursor']==recent.isoformat() and not progress['done']
        assert progress['current_gap_at']==(opened+timedelta(minutes=2)).isoformat()
    finally:repo.close()


async def test_initial_empty_history_is_retried_after_real_bars_arrive(tmp_path):
    from sessionquant.market.source import TossSessionSource
    repo,_,_,_,manager = context(tmp_path)
    try:
        await manager.refresh(WATCH_NOW);observe(manager)
        stamp = WATCH_NOW+timedelta(seconds=60)
        class Later(Market):
            ready=False
            async def candles(self,symbol,**kwargs):
                self.calls.append(('candles',symbol))
                if not self.ready:return [],None
                return [Bar(symbol,stamp,stamp,100,101,99,100,10)],(stamp-timedelta(minutes=1)).isoformat()
        client = Later(lambda:stamp)
        source = TossSessionSource(client,clock=lambda:stamp,watchlist=manager,history_pages_per_cycle=0)
        await source.poll([],priority_symbols=['T001'])
        assert manager.state['history_progress']['T001']['status']=='WAITING_DATA'
        stamp += timedelta(seconds=61);client.ready=True
        await source.poll([],priority_symbols=['T001'])
        assert not manager.state['history_progress']['T001']['done']
        assert manager.state['history_progress']['T001']['cursor'] is not None
    finally:repo.close()
