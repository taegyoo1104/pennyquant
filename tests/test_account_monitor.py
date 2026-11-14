"""모의계좌와 분리된 실제 계좌 조회에서 값·권한·오류 표시를 검증한다."""
import copy
from datetime import datetime,timezone
import json
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest

from sessionquant.account_monitor import fetch_account_snapshot,run_account_monitor
from sessionquant.collectors.toss_market import TossMarketClient
from sessionquant.database.repository import Repository


def holdings():
    return {'marketValue':{'amount':{'usd':'1200.50','krw':'350000'}},'totalPurchaseAmount':{'usd':'1100','krw':'300000'},
        'items':[{'symbol':'AAPL','name':'Apple','currency':'USD','marketCountry':'US','quantity':'0.123456',
            'lastPrice':'190','averagePurchasePrice':'180','marketValue':{'amount':'23.45664'},'profitLoss':{'amount':'1.23456'}},
            {'symbol':'005930','name':'삼성전자','currency':'KRW','marketCountry':'KR','quantity':'5',
                'lastPrice':'70000','averagePurchasePrice':'60000','marketValue':{'amount':'350000'},'profitLoss':{'amount':'50000'}}]}


@pytest.fixture
async def api():
    state={'requests':[],'cash_error':False,'orders_error':False,'accounts':[{'accountSeq':7,'accountType':'BROKERAGE','accountNo':'PRIVATE_ACCOUNT'}]}
    def handle(request):
        path=request.url.path; state['requests'].append((request.method,path))
        if path=='/oauth2/token': return httpx.Response(200,json={'access_token':'FAKE_TOKEN','expires_in':3600})
        if path=='/api/v1/accounts': result=state['accounts']
        elif path=='/api/v1/holdings': result=holdings()
        elif path=='/api/v1/buying-power':
            if state['cash_error']: return httpx.Response(403)
            currency=request.url.params['currency']
            result={'currency':currency,'cashBuyingPower':'321.45' if currency=='USD' else '1234567'}
        elif path=='/api/v1/orders':
            if state['orders_error']: return httpx.Response(403)
            result={'orders':[],'hasNext':False}
        else: pytest.fail('주문 또는 예상하지 않은 URL 호출: '+path)
        return httpx.Response(200,json={'result':result})
    http=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client=TossMarketClient(client_id='mock-id',client_secret='mock-secret',client=http)
    client.account_seq=None
    async def no_wait(): pass
    client.limiter=SimpleNamespace(wait=no_wait)
    yield client,state
    await http.aclose()


async def test_actual_mixed_holdings_are_exact_and_only_auth_posts(api):
    client,state=api
    snapshot=await fetch_account_snapshot(client)
    assert snapshot['mode']=='account_readonly' and snapshot['synthetic'] is False
    assert snapshot['buying_power']['USD']=='321.45'
    assert [row['quantity'] for row in snapshot['items']]==['0.123456','5']
    assert snapshot['items'][1]['currency']=='KRW'
    assert 'PRIVATE_ACCOUNT' not in json.dumps(snapshot)
    assert [path for method,path in state['requests'] if method=='POST']==['/oauth2/token']


async def test_partial_api_failure_is_unknown_not_fake_zero(api):
    client,state=api; state['cash_error']=True; state['orders_error']=True
    snapshot=await fetch_account_snapshot(client)
    assert snapshot['buying_power']=={'USD':None,'KRW':None}
    assert snapshot['open_orders'] is None and len(snapshot['issues'])==3
    assert len(snapshot['items'])==2


async def test_multiple_accounts_require_explicit_selection(api):
    client,state=api
    state['accounts'].append({'accountSeq':8,'accountType':'BROKERAGE'})
    with pytest.raises(ValueError,match='TOSS_ACCOUNT_SEQ'): await fetch_account_snapshot(client)
    assert all(path!='/api/v1/holdings' for method,path in state['requests'])


async def test_selected_account_must_be_accessible(api):
    client,state=api; client.account_seq=999
    with pytest.raises(ValueError,match='확인하지'): await fetch_account_snapshot(client)


async def test_monitor_persists_latest_without_reusing_paper_state(tmp_path,monkeypatch,api):
    import sessionquant.account_monitor as module
    client,state=api
    monkeypatch.setattr(module,'TossMarketClient',lambda:client)
    real_repo=Repository
    monkeypatch.setattr(module,'Repository',lambda path,backend=None:real_repo(path,backend='sqlite'))
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite'); monkeypatch.setenv('SQ_ENVIRONMENT','PAPER_LOCAL')
    monkeypatch.delenv('SQ_DB_NAMESPACE',raising=False); monkeypatch.delenv('PQ_DB_NAMESPACE',raising=False)
    async def no_wait(*args): pass
    monkeypatch.setattr(module.asyncio,'sleep',no_wait)
    path=tmp_path/'account.db'
    await run_account_monitor(db_path=path,no_dashboard=True,max_polls=2)
    repo=real_repo(path,backend='sqlite')
    assert repo.get_control('actual_account_snapshot')['buying_power']['USD']=='321.45'
    assert repo.load_state() is None and repo.all('orders')==[] and repo.all('executions')==[]
    assert len(repo.all('account_snapshots'))==1
    repo.close()
    paper=tmp_path/'paper.db'; repo=real_repo(paper,backend='sqlite'); repo.save_state({'cash':70}); repo.close()
    with pytest.raises(ValueError,match='기존'):
        await run_account_monitor(db_path=paper,no_dashboard=True,max_polls=1)
    repo=real_repo(paper,backend='sqlite'); assert repo.load_state()=={'cash':70}; repo.close()


def test_dashboard_uses_reported_amounts_and_has_no_order_controls(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    path=tmp_path/'account.db'
    repo=Repository(path,backend='sqlite')
    repo.set_control('actual_account_snapshot',{'received_at':datetime.now(timezone.utc).isoformat(),
        'items':holdings()['items'],'summary':holdings(),'buying_power':{'USD':'321.45','KRW':None},'open_orders':None,'issues':[]})
    repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite')
    monkeypatch.setattr('sys.argv',['account_app.py','--db',str(path)])
    app=AppTest.from_file(str(Path(__file__).parents[1]/'sessionquant/dashboard/account_app.py')).run(timeout=15)
    assert not app.exception and not app.error
    assert [metric.value for metric in app.metric]==['$1,200.50','$321.45','₩350,000','—']
    assert not app.button
