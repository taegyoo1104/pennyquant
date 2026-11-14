"""토스 실제 계좌를 읽기 전용으로 조회한다. 주문 어댑터를 사용하지 않는다."""
from __future__ import annotations
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

import httpx

from .collectors.toss_market import TossMarketClient
from .collectors.common import DataQualityError
from .database.repository import Repository
from .locking import exclusive_lock
from .notifications import dashboard_environment
from .portfolio.models import decimal


def amounts(value):
    """보고된 숫자는 정확한 문자열로 보존하고 미제공 값은 None으로 남긴다."""
    if value is None: return None
    if isinstance(value,dict): return {k:amounts(v) for k,v in value.items()}
    return str(decimal(value))


async def fetch_account_snapshot(client):
    accounts=await client.accounts()
    eligible=[a for a in accounts if a.get('accountType')=='BROKERAGE']
    if not eligible: raise ValueError('조회 가능한 종합매매 계좌가 없습니다.')
    if client.account_seq is None:
        if len(eligible)!=1: raise ValueError('종합매매 계좌가 여러 개면 .env에 TOSS_ACCOUNT_SEQ를 지정하세요.')
        client.account_seq=eligible[0]['accountSeq']
    if not any(a.get('accountSeq')==client.account_seq for a in eligible):
        raise ValueError('선택한 종합매매 계좌를 확인하지 못했습니다.')
    start=datetime.now(timezone.utc)
    source=await client.holdings()
    if not isinstance(source,dict) or not isinstance(source.get('items'),list):
        raise DataQualityError('토스 보유 주식 응답 형식을 확인하지 못했습니다.')
    items=[]
    for row in source['items']:
        currency=row['currency']
        if currency not in {'USD','KRW'}: raise DataQualityError('지원하지 않는 보유 통화입니다.')
        item={k:row.get(k) for k in ('symbol','name','marketCountry','currency')}
        item.update({k:amounts(row.get(k)) for k in ('quantity','lastPrice','averagePurchasePrice','marketValue','profitLoss','dailyProfitLoss','cost')})
        if item['quantity'] is None or decimal(item['quantity'])<0: raise DataQualityError('보유 수량 오류')
        items.append(item)
    cash={}; issues=[]
    for currency in ('USD','KRW'):
        try:
            result=await client._get('/api/v1/buying-power',params={'currency':currency},account=True)
            if result.get('currency')!=currency: raise DataQualityError('매수 가능 금액 통화 불일치')
            cash[currency]=str(decimal(result['cashBuyingPower'],nonnegative=True))
        except (httpx.HTTPError,RuntimeError,ValueError,KeyError,TypeError):
            cash[currency]=None; issues.append(currency+' 매수 가능 금액 조회 실패')
    orders=None
    try:
        result=await client.orders('OPEN')
        if result.get('hasNext') is not False or not isinstance(result.get('orders'),list):
            raise DataQualityError('미체결 목록 완전성 미확인')
        orders=[{k:o.get(k) for k in ('orderId','symbol','side','orderType','status','currency','price','quantity','orderAmount','orderedAt')}
                for o in result['orders']]
    except (httpx.HTTPError,RuntimeError,ValueError,KeyError,TypeError):
        issues.append('미체결 주문 조회 실패')
    return {'id':start.isoformat(),'started_at':start.isoformat(),'received_at':datetime.now(timezone.utc).isoformat(),
        'account_seq':client.account_seq,'source':'toss_actual_account','mode':'account_readonly','synthetic':False,
        'live_orders_enabled':False,'items':items,'buying_power':cash,'open_orders':orders,'issues':issues,
        'summary':{k:amounts(source.get(k)) for k in ('totalPurchaseAmount','marketValue','profitLoss','dailyProfitLoss')}}


async def run_account_monitor(*,db_path=Path('data/sessionquant-account.db'),interval=10,port=8502,no_dashboard=False,max_polls=None):
    if type(interval) is not int or not 5<=interval<=3600: raise ValueError('계좌 갱신 주기는 5~3600초입니다.')
    if type(port) is not int or not 1024<=port<=65535: raise ValueError('대시보드 포트 범위 오류')
    if max_polls is not None and (type(max_polls) is not int or max_polls<1): raise ValueError('max_polls는 1 이상이어야 합니다.')
    if os.getenv('SQ_DB_NAMESPACE') or os.getenv('PQ_DB_NAMESPACE'):
        raise ValueError('계좌 조회에는 별도 DB 경로를 사용하고 DB_NAMESPACE 덮어쓰기를 해제하세요.')
    if not no_dashboard:
        import socket
        try:
            with socket.socket() as probe: probe.bind(('127.0.0.1',port))
        except OSError:
            raise ValueError(f'{port} 포트가 사용 중입니다. 기존 화면을 종료하거나 --port로 다른 포트를 지정하세요.') from None
    os.environ['SQ_DB_BACKEND']='mysql'; os.environ['SQ_ENVIRONMENT']='LIVE_LOCAL'
    client=TossMarketClient(); dashboard=None
    try:
        with ExitStack() as stack:
            stack.enter_context(exclusive_lock(str(db_path)+'.engine.lock',message='이미 같은 계좌 조회가 실행 중입니다.'))
            repo=Repository(db_path,backend='mysql'); stack.callback(repo.close)
            stack.enter_context(repo.engine_lease())
            runtime=repo.get_control('runtime_kind')
            if runtime!='account_monitor' and (repo.namespace_has_data() or repo.load_state() is not None):
                raise ValueError('기존 매매·모의 기록 대신 새로운 계좌 조회용 DB를 사용하세요.')
            repo.set_control('runtime_kind','account_monitor')
            polls=0
            while max_polls is None or polls<max_polls:
                try:
                    snapshot=await fetch_account_snapshot(client)
                    bound=repo.get_control('account_monitor_account')
                    if bound is not None and bound!=client.account_seq: raise ValueError('다른 계좌를 조회하려면 별도 DB 경로를 지정하세요.')
                    with repo.transaction():
                        repo.set_control('account_monitor_account',client.account_seq)
                        repo.set_control('actual_account_snapshot',snapshot)
                        repo.set_control('account_monitor_status',{'status':'ok','updated_at':snapshot['received_at'],'issues':snapshot['issues']})
                        repo.record('account_snapshots',{**snapshot,'id':'actual_account_latest'})
                    print(f"실제 계좌 조회 완료: 보유 {len(snapshot['items'])}종목 · 주문 기능 없음",flush=True)
                except (httpx.HTTPError,DataQualityError) as exc:
                    repo.set_control('account_monitor_status',{'status':'error','updated_at':datetime.now(timezone.utc).isoformat(),
                        'message':'계좌 갱신 실패. 이전 조회 시각과 토스 연결을 확인하세요.','error_type':type(exc).__name__})
                    if polls==0: raise
                if not no_dashboard and dashboard is None:
                    app=Path(__file__).parent/'dashboard'/'account_app.py'
                    dashboard=subprocess.Popen([sys.executable,'-m','streamlit','run',str(app),'--server.address','127.0.0.1',
                        '--server.port',str(port),'--server.headless','true','--','--db',str(db_path)],env=dashboard_environment(database=True))
                    print(f'실제 계좌 화면: http://127.0.0.1:{port}',flush=True)
                polls+=1
                if max_polls is None or polls<max_polls: await asyncio.sleep(interval)
    finally:
        if dashboard:
            dashboard.terminate()
            try: dashboard.wait(timeout=5)
            except subprocess.TimeoutExpired:
                dashboard.kill(); dashboard.wait(timeout=5)
        await client.aclose()
    return 0
