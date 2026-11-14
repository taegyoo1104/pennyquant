"""실제 계좌 대신 모의 HTTP로 접수·체결·대사·복구 안전성을 검증한다."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import asyncio
import copy
import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.live.broker import FractionalLiveBroker,FractionalRequest
from sessionquant.collectors.common import CapabilityUnavailable,DataQualityError
from sessionquant.live.runtime import load_live_config,entry_blockers,net_state

NOW=datetime(2026,11,13,16,tzinfo=timezone.utc)

class FakeMarket:
    def __init__(self):
        self.account_seq=7; self.cash='100'; self.positions={}; self.opened=[]; self.details={}; self.posts=[]; self.failure=None
        self.limiter=SimpleNamespace(wait=self.wait)
        self.client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
    async def wait(self): pass
    async def access_token(self): return 'MOCK_TOKEN'
    async def accounts(self): return [{'accountSeq':7,'accountType':'BROKERAGE'}]
    async def holdings(self): return {'items':[{'symbol':s,'quantity':q,'marketCountry':'US','currency':'USD'} for s,q in self.positions.items()]}
    async def orders(self,status): return {'orders':self.opened,'hasNext':False}
    async def _get(self,path,**kwargs):
        if path=='/api/v1/buying-power': return {'currency':'USD','cashBuyingPower':self.cash}
        if path=='/api/v1/sellable-quantity': return {'sellableQuantity':self.positions.get(kwargs['params']['symbol'],'0')}
        return copy.deepcopy(self.details[path.rsplit('/',1)[1]])
    def handle(self,request):
        import json
        self.posts.append((request.url.path,json.loads(request.content)))
        if self.failure=='timeout': raise httpx.ReadTimeout('mock',request=request)
        if self.failure=='server': return httpx.Response(503)
        if self.failure=='rejected': return httpx.Response(422,json={'error':{'code':'test'}})
        if request.url.path.endswith('/cancel'): return httpx.Response(200,json={'result':{'orderId':'cancel-1'}})
        payload=json.loads(request.content)
        return httpx.Response(200,json={'result':{'orderId':'broker-'+str(len(self.posts)),'clientOrderId':payload['clientOrderId']}})

@pytest.fixture
async def setup(tmp_path):
    repo=Repository(tmp_path/'live.db',backend='sqlite'); market=FakeMarket()
    broker=FractionalLiveBroker(market,repo,armed=True,clock=lambda:NOW)
    yield repo,market,broker
    await market.client.aclose(); repo.close()

def buy(cid='buy-1'): return FractionalRequest(cid,'AAPL','BUY',amount='1.00')

def detail(qty='0.01',amount='1.00',status='FILLED',fee='0.01'):
    return {'orderId':'broker-1','symbol':'AAPL','side':'BUY','currency':'USD','orderType':'MARKET','timeInForce':'DAY',
        'orderAmount':'1.00','quantity':'0','status':status,'execution':{'filledQuantity':qty,'filledAmount':amount,
            'averageFilledPrice':'100','commission':fee,'tax':'0','filledAt':NOW.isoformat()}}

@pytest.mark.parametrize('args',[{'side':'BUY','amount':'NaN'},{'side':'BUY','amount':'1.001'},
    {'side':'SELL','quantity':'0.0000001'},{'side':'SELL','quantity':'0'},
    {'side':'BUY','amount':'1','quantity':'1'},{'side':'BUY','amount':'1e100'}])
def test_invalid_request_never_reaches_network(args):
    with pytest.raises(ValueError): FractionalRequest('id','AAPL',**args)

async def test_readonly_reconcile_never_posts_or_persists_activation(setup):
    repo,m,b=setup; b.armed=False
    assert await b.reconcile()
    with pytest.raises(CapabilityUnavailable): await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    assert not m.posts
    assert repo.get_control('broker_state')['live_orders_enabled'] is False

async def test_create_is_durable_before_post_and_duplicate_never_resent(setup):
    repo,m,b=setup; await b.reconcile()
    original=m.handle
    def handle(request):
        assert repo.get_control('fractional_live_v1')['orders']['buy-1']['status']=='SUBMITTING'
        return original(request)
    m.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    assert len(m.posts)==1 and m.posts[0][1]['orderAmount']=='1.00'
    assert 'quantity' not in m.posts[0][1]
    with pytest.raises(ValueError): await b.submit_order(FractionalRequest('buy-1','AAPL','BUY',amount='2'),buy_cap='5')

@pytest.mark.parametrize('failure',['timeout','server'])
async def test_ambiguous_post_blocks_restart_and_fresh_ids(setup,failure):
    repo,m,b=setup; await b.reconcile(); m.failure=failure
    with pytest.raises((httpx.HTTPError,RuntimeError)): await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    restored=FractionalLiveBroker(m,repo,armed=True,clock=lambda:NOW)
    assert not await restored.reconcile()
    with pytest.raises(CapabilityUnavailable): await restored.submit_order(buy('new-id'),buy_cap='5',eligibility_verified=True)
    assert len(m.posts)==1

async def test_db_failure_prevents_any_create_post(setup,monkeypatch):
    repo,m,b=setup; await b.reconcile()
    monkeypatch.setattr(repo,'set_control',lambda *a: (_ for _ in ()).throw(RuntimeError('database failed')))
    with pytest.raises(RuntimeError): await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    assert not m.posts and b.failed and not b.armed

async def test_cumulative_fill_and_late_fees_are_upserted_not_duplicated(setup):
    repo,m,b=setup; await b.reconcile(); await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    m.details['broker-1']=detail(qty='.005',amount='.50',status='PARTIAL_FILLED',fee=None)
    await b.get_order('buy-1')
    assert b.owned_quantities()['AAPL']==Decimal('.005')
    m.details['broker-1']=detail()
    await b.get_order('buy-1'); await b.get_order('buy-1')
    assert len(repo.all('executions'))==1
    assert repo.all('executions')[0]['fee']=='0.01'
    assert b.owned_quantities()['AAPL']==Decimal('.01')
    assert net_state(b,Decimal('100'))['net_pnl']==Decimal('-.01')

@pytest.mark.parametrize('change',[{'currency':'KRW'},{'orderAmount':'2'},{'orderType':'LIMIT'},{'status':'UNRECOGNIZED'}])
async def test_mismatched_order_detail_cannot_enable_trading(setup,change):
    repo,m,b=setup; await b.reconcile(); await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    m.details['broker-1']={**detail(),**change}
    with pytest.raises((ValueError,RuntimeError)): await b.reconcile()
    assert not b.reconciled

async def test_external_holdings_are_preserved_and_mismatch_blocks(setup):
    repo,m,b=setup; m.positions={'MSFT':'1.5'}
    assert await b.reconcile()
    assert b.state['baseline']=={'MSFT':'1.5'}
    m.positions={'MSFT':'1.6'}
    assert not await b.reconcile() and not m.posts

async def test_external_open_order_blocks_initial_trade(setup):
    repo,m,b=setup; m.opened=[{'orderId':'external'}]
    assert not await b.reconcile()
    assert 'external_open_order' in b.state['halt_reasons']

async def test_restart_during_submit_becomes_unknown_and_is_not_retried(setup):
    repo,m,b=setup; b.orders['buy-1']={'request':buy().payload(),'status':'SUBMITTING','created_at':NOW.isoformat()}
    b._save(); r=FractionalLiveBroker(m,repo,armed=True,clock=lambda:NOW)
    assert r.orders['buy-1']['status']=='UNKNOWN'
    assert not await r.reconcile() and not m.posts

async def test_new_instance_is_unarmed_and_rejects_other_account(setup):
    repo,m,b=setup; await b.reconcile()
    assert not FractionalLiveBroker(m,repo).armed
    m.account_seq=8
    with pytest.raises(ValueError,match='계좌'): FractionalLiveBroker(m,repo)

async def test_session_cutoff_prevents_post(setup):
    repo,m,b=setup; await b.reconcile(); b.clock=lambda:NOW.replace(hour=21)
    with pytest.raises(CapabilityUnavailable): await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    assert not m.posts

async def test_oversell_and_unverified_buy_are_blocked(setup):
    repo,m,b=setup; await b.reconcile()
    with pytest.raises(CapabilityUnavailable): await b.submit_order(buy(),buy_cap='5')
    with pytest.raises(CapabilityUnavailable):
        await b.submit_order(FractionalRequest('sell-1','AAPL','SELL',quantity='.01'),buy_cap='5')
    assert not m.posts

def test_live_config_defaults_cannot_enable_orders():
    config=load_live_config('config/live.local.yaml')
    blockers=entry_blockers(config,NOW)
    assert any('최소 주문' in x for x in blockers)
    assert any('자격' in x or '소수점' in x for x in blockers)

async def test_fractional_roundtrip_books_only_broker_fills(setup):
    repo,m,b=setup; await b.reconcile()
    await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    m.details['broker-1']=detail(); m.positions={'AAPL':'.01'}
    assert await b.reconcile()
    assert repo.all('positions')[0]['qty']=='0.01'
    await b.submit_order(FractionalRequest('sell-1','AAPL','SELL',quantity='.01'),buy_cap='5')
    m.details['broker-2']={**detail(amount='1.02'),'orderId':'broker-2','side':'SELL','orderAmount':None,'quantity':'.01',
        'execution':{**detail()['execution'],'averageFilledPrice':'102','filledAmount':'1.02'}}
    m.positions={}
    assert await b.reconcile()
    assert b.owned_quantities()=={} and len(repo.all('executions'))==2
    assert repo.all('positions')==[]
    assert net_state(b,Decimal('102'))['net_pnl']==0

async def test_cancel_timeout_is_not_resent_after_restart(setup):
    repo,m,b=setup; await b.reconcile(); await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    m.details['broker-1']=detail(qty='0',amount='0',status='PENDING',fee=None)
    assert await b.reconcile()
    m.failure='timeout'
    with pytest.raises(httpx.ReadTimeout): await b.cancel_order('buy-1')
    restored=FractionalLiveBroker(m,repo,armed=True,clock=lambda:NOW)
    assert not await restored.reconcile()
    await restored.cancel_order('buy-1')
    assert len(m.posts)==2

async def test_missing_fees_remain_pending_not_final_profit(setup):
    repo,m,b=setup; await b.reconcile(); await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    m.details['broker-1']=detail(fee=None)
    await b.get_order('buy-1')
    assert net_state(b,Decimal('100'))['fees_pending'] is True
    assert repo.all('executions')[0]['fees_final'] is False

@pytest.mark.parametrize("execute",[False,True])
async def test_live_runtime_roundtrip_and_flags_with_mock_account(tmp_path,monkeypatch,execute):
    """실계좌 없이 실제 런타임의 활성화·매수·시간 청산·대사 경로를 연결한다."""
    import yaml
    import sessionquant.live.runtime as runtime
    from sessionquant.market.universe import UniverseStore
    from sessionquant.models import Quote
    class Clock(datetime):
        count=0
        @classmethod
        def now(cls,tz=None):
            cls.count+=1
            return NOW+timedelta(seconds=cls.count)
    monkeypatch.setattr(runtime,'datetime',Clock)
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite')
    monkeypatch.setenv('SQ_ENVIRONMENT','LIVE_LOCAL')
    monkeypatch.delenv('SQ_DB_NAMESPACE',raising=False)
    monkeypatch.delenv('PQ_DB_NAMESPACE',raising=False)
    m=FakeMarket()
    original_handle=m.handle
    def handler(request):
        response=original_handle(request)
        import json
        payload=json.loads(request.content)
        oid='broker-'+str(len(m.posts))
        if not request.url.path.endswith('/cancel'):
            d=detail()
            d['orderId']=oid
            d['execution']['filledAt']=Clock.now().isoformat()
            if payload['side']=='SELL':
                d.update(side='SELL',orderAmount=None,quantity='.01'); m.positions={}
            else: m.positions={'AAPL':'.01'}
            m.details[oid]=d
        return response
    await m.client.aclose()
    m.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async def stocks(symbols): return [{'symbol':'AAPL','currency':'USD','status':'ACTIVE','isCommonShare':True,'market':'NASDAQ'}]
    async def quote(*args,**kwargs):
        now=Clock.now()
        return Quote('AAPL',now,now,100,100.01,1000,1000,True,False,'NASDAQ')
    original_get=m._get
    async def get(path,**kwargs):
        return [] if path=='/api/v1/commissions' else await original_get(path,**kwargs)
    m.stocks=stocks; m.quote=quote; m._get=get
    m.aclose=m.client.aclose
    monkeypatch.setattr(runtime,'TossMarketClient',lambda:m)
    from sessionquant.database.repository import Repository as RealRepository
    def repository(path,backend=None):
        r=RealRepository(path,backend='sqlite'); r._execute=lambda *args,**kwargs:(1,)
        original_set=r.set_control
        ticks={'count':0,'queued':False}
        def set_control(name,value):
            original_set(name,value)
            if name=='trade_control_state':
                ticks['count']+=1
                if ticks['count']==2 and not ticks['queued'] and value.get('armed'):
                    assert not m.posts, '시작 버튼 전에는 실제 주문을 보내지 않는다'
                    ticks['queued']=True
                    from sessionquant.trading_controls import enqueue
                    enqueue(r,'start',run_id=value['run_id'],account_seq=7,now=Clock.now())
        r.set_control=set_control
        return r
    monkeypatch.setattr(runtime,'Repository',repository)
    monkeypatch.setattr(runtime.subprocess,'Popen',lambda *a,**kw:SimpleNamespace(terminate=lambda:None,wait=lambda **kw:0,kill=lambda:None))
    async def no_wait(*args): pass
    monkeypatch.setattr(runtime.asyncio,'sleep',no_wait)
    universe=tmp_path/'universe.json'
    UniverseStore(universe).refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=NOW-timedelta(days=1))
    data=yaml.safe_load(open('config/live.local.yaml'))
    data.update(db_path=str(tmp_path/'live-test.db'),universe_path=str(universe),minimum_amount='1.00',
                eligibility_checked_at=NOW.isoformat(),eligibility_evidence='모의 계좌 자격을 명시한 검증 자료',max_hold_seconds=30,poll_seconds=2)
    config=tmp_path/'live.yaml'; config.write_text(yaml.safe_dump(data))
    # 런타임과 브로커는 동일한 실제 벽시계를 사용한다. 검증에서는 같은 고정 시계를 주입한다.
    original_broker=runtime.FractionalLiveBroker
    monkeypatch.setattr(runtime,'FractionalLiveBroker',lambda market,repo,armed=False:original_broker(market,repo,armed=armed,clock=lambda:Clock.now()))
    result=await runtime.live_local(config,execute=execute,confirm_account_seq=7,acknowledge=execute,max_polls=20,dashboard=True)
    assert result['closed'] is True and result['mode']=='live_test_paused'
    assert [p[1]['side'] for p in m.posts]==(['BUY','SELL'] if execute else [])
    assert result['actual_order_permissions_verified'] is execute
    r=RealRepository(tmp_path/'live-test.db',backend='sqlite')
    assert len(r.all('executions'))==(2 if execute else 0) and not r.all('positions')
    r.close()

async def test_explicit_external_position_sale_preserves_managed_quantities(setup):
    repo,m,b=setup; m.positions={'MSFT':'0.01'}
    assert await b.reconcile()
    with pytest.raises(CapabilityUnavailable):
        await b.submit_order(FractionalRequest('unauthorized','MSFT','SELL',quantity='.01'),buy_cap='5')
    await b.submit_order(FractionalRequest('manual-external','MSFT','SELL',quantity='.01'),buy_cap='5',external_position=True)
    m.details['broker-1']={**detail(),'symbol':'MSFT','side':'SELL','orderAmount':None,'quantity':'.01'}
    m.positions={}
    assert await b.reconcile()
    assert b.owned_quantities()=={}
    assert net_state(b,Decimal('100'))['net_pnl']==0
    restored=FractionalLiveBroker(m,repo,armed=True,clock=lambda:NOW)
    assert await restored.reconcile()
    assert restored.state['baseline']=={'MSFT':'0.01'}

async def test_late_buy_is_blocked_at_actual_post_boundary(setup):
    repo,m,b=setup; await b.reconcile()
    b.clock=lambda:NOW.replace(hour=18,minute=29,second=59)
    b.observed_at=b.clock().isoformat()
    async def cross_cutoff(): b.clock=lambda:NOW.replace(hour=18,minute=30)
    m.limiter=SimpleNamespace(wait=cross_cutoff)
    with pytest.raises(CapabilityUnavailable,match='마감'):
        await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    assert not m.posts

async def test_stop_queued_after_durable_intent_prevents_create_post(setup,monkeypatch):
    from sessionquant.trading_controls import begin_session,enqueue,heartbeat
    repo,m,b=setup; await b.reconcile()
    state=begin_session(repo,mode='live',account_seq=7,now=NOW)
    state['running']=True; heartbeat(repo,state,now=NOW)
    original=repo.set_control; queued={'value':False}
    def set_control(name,value):
        original(name,value)
        if name=='fractional_live_v1' and not queued['value'] and any(i['status']=='SUBMITTING' for i in value['orders'].values()):
            queued['value']=True
            enqueue(repo,'stop_all',run_id=state['run_id'],account_seq=7,now=NOW)
    monkeypatch.setattr(repo,'set_control',set_control)
    order=await b.submit_order(buy(),buy_cap='5',eligibility_verified=True)
    assert order['status']=='CANCELED' and order['local_cancel_before_submit'] is True
    assert not m.posts
