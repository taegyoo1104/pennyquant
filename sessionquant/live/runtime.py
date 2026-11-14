"""읽기 전용 사전 검사와 1종목·1회 매수로 제한한 로컬 실주문 검증."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import asyncio
import os
from uuid import uuid4
import yaml
import subprocess
import sys

from ..collectors.common import CapabilityUnavailable
from ..collectors.toss_market import TossMarketClient
from ..database.repository import Repository
from ..market.bars import session_bounds
from ..market.universe import UniverseStore
from ..models import utc,symbol_valid
from ..notifications import DesktopNotifier
from ..portfolio.models import decimal
from .broker import FractionalLiveBroker,FractionalRequest,TERMINAL

FIELDS={'environment','db_path','universe_path','symbol','buy_amount','max_buy_amount','strategy_cash',
        'stop_pct','target_pct','daily_loss_pct','risk_per_symbol','max_hold_seconds','poll_seconds',
        'minimum_amount','eligibility_checked_at','eligibility_evidence','min_entry_to_exit_minutes'}

def load_live_config(path):
    path=Path(path).resolve(); data=yaml.safe_load(path.read_text(encoding='utf-8'))
    if isinstance(data,dict): data.setdefault("min_entry_to_exit_minutes",30)
    if not isinstance(data,dict) or set(data)!=FIELDS or data['environment']!='LIVE_LOCAL':
        raise ValueError('config/live.local.yaml의 LIVE_LOCAL 설정 항목을 확인하세요.')
    symbol_valid(data['symbol'])
    if type(data['min_entry_to_exit_minutes']) is not int or data['min_entry_to_exit_minutes']<30:
        raise ValueError('청산 전 진입 여유는 최소 30분입니다.')
    for name in ('buy_amount','max_buy_amount','strategy_cash','stop_pct','target_pct','daily_loss_pct','risk_per_symbol'):
        data[name]=decimal(data[name],name,positive=True)
    if not data['buy_amount']<=data['max_buy_amount']<=Decimal('25'):
        raise ValueError('초기 소액 검증 주문 한도는 최대 25달러입니다.')
    if data['buy_amount']!=data['buy_amount'].quantize(Decimal('.01')): raise ValueError('매수 금액은 센트 단위로 입력하세요.')
    for name in ('stop_pct','target_pct','daily_loss_pct','risk_per_symbol'):
        if data[name]>=1: raise ValueError('위험·목표 비율은 1 미만이어야 합니다.')
    for name,low,high in [('poll_seconds',2,60),('max_hold_seconds',30,1800)]:
        if type(data[name]) is not int or not low<=data[name]<=high: raise ValueError(f'{name} 범위 오류')
    for name in ('db_path','universe_path'):
        data[name]=(path.parent/str(data[name])).resolve()
    if data['minimum_amount'] is not None: data['minimum_amount']=decimal(data['minimum_amount'],positive=True)
    return data

def entry_blockers(config,now):
    issues=[]
    if config['minimum_amount'] is None: issues.append('계좌 최소 주문 금액 미확인')
    elif config['buy_amount']<config['minimum_amount']: issues.append('최소 주문 금액 미달')
    try:
        checked=utc(config['eligibility_checked_at'])
        if not checked<=now<=checked+timedelta(minutes=30): issues.append('계좌별 종목 자격 확인이 30분 이내가 아님')
    except (ValueError,TypeError,AttributeError): issues.append('계좌별 소수점 지원·거래정지·기업행동 확인 시각 필요')
    if not isinstance(config['eligibility_evidence'],str) or len(config['eligibility_evidence'].strip())<10:
        issues.append('계좌 자격 확인 근거 필요')
    bounds=session_bounds(now)
    from ..session_window import permits_new_entry
    if not permits_new_entry(now,min_entry_to_exit_minutes=config.get('min_entry_to_exit_minutes',30)):
        issues.append('신규 진입 시간(정규장 10:45~13:30)이 아님')
    if not UniverseStore(config['universe_path']).contains(config['symbol'],now): issues.append('최신 S&P 500 편입 미확인')
    # 최소 수수료·가격 급변 여유를 포함한 첫 실험의 보수적 예상 손실 한도.
    if config['buy_amount']*(config['stop_pct']+Decimal('.02'))+Decimal('.05')>config['strategy_cash']*config['risk_per_symbol']:
        issues.append('종목별 예상 손실 예산 초과')
    return issues

def net_state(broker,bid=None):
    invested=proceeds=fees=Decimal(0); missing=False
    for row in broker.orders.values():
        if row.get('ownership')=='external': continue
        amount=decimal(row.get('filled_amount','0'))
        if row['request']['side']=='BUY': invested+=amount
        else: proceeds+=amount
        if decimal(row.get('filled_quantity','0')) and (row.get('commission') is None or row.get('tax') is None): missing=True
        fees+=decimal(row.get('commission') or '0')+decimal(row.get('tax') or '0')
    qty=sum(broker.owned_quantities().values(),Decimal(0))
    net=proceeds-invested-fees+(qty*bid if bid is not None else Decimal(0))
    return {'invested':invested,'proceeds':proceeds,'fees':fees,'qty':qty,'net_pnl':net,'fees_pending':missing}

async def live_local(config_path,*,execute=False,confirm_account_seq=None,acknowledge=False,max_polls=None,dashboard=False,port=8502):
    config=load_live_config(config_path)
    dashboard=bool(dashboard or execute)
    if dashboard:
        import socket
        if type(port) is not int or not 1024<=port<=65535: raise ValueError("대시보드 포트 범위 오류")
        try:
            with socket.socket() as probe: probe.bind(("127.0.0.1",port))
        except OSError: raise ValueError("대시보드 포트가 사용 중입니다. 기존 화면을 종료하거나 다른 포트를 지정하세요.") from None
    if max_polls is not None and (type(max_polls) is not int or max_polls<1): raise ValueError("max_polls는 1 이상이어야 합니다.")
    if execute and (acknowledge is not True or confirm_account_seq is None):
        raise ValueError('실주문은 --execute --acknowledge-live-orders --confirm-account-seq 값이 필요합니다.')
    os.environ['SQ_DB_BACKEND']='mysql'; os.environ['SQ_ENVIRONMENT']='LIVE_LOCAL'
    if 'paper' in config['db_path'].stem.lower() or os.getenv('SQ_DB_NAMESPACE') or os.getenv('PQ_DB_NAMESPACE'):
        raise ValueError('실거래 전용 DB 경로를 사용하고 SQ_DB_NAMESPACE 수동 덮어쓰기를 해제하세요.')
    market=TossMarketClient(); broker=None; display=None
    try:
        accounts=await market.accounts()
        selectable=[a for a in accounts if a.get('accountType')=='BROKERAGE']
        if market.account_seq is None:
            if len(selectable)!=1: raise ValueError('본인 PC에 TOSS_ACCOUNT_SEQ로 사용할 계좌를 선택하세요.')
            market.account_seq=selectable[0]['accountSeq']
        if execute and market.account_seq!=confirm_account_seq: raise ValueError('실주문 확인 계좌와 선택 계좌가 다릅니다.')
        with ExitStack() as stack:
            repo=Repository(config['db_path'],backend='mysql'); stack.callback(repo.close)
            stack.enter_context(repo.engine_lease())
            # 같은 MySQL 서버의 다른 DB·네임스페이스에서도 같은 계좌를 동시에 실행하지 못한다.
            name='sessionquant-account:'+sha256(str(market.account_seq).encode()).hexdigest()[:40]
            lock=repo._execute('SELECT GET_LOCK(%s,0)',(name,),fetch='one')
            if not lock or lock[0]!=1: raise RuntimeError('다른 실거래 엔진이 같은 계좌를 사용 중입니다.')
            stack.callback(lambda:repo._execute('SELECT RELEASE_LOCK(%s)',(name,),fetch='one'))
            saved=repo.get_control('runtime_kind')
            if repo.load_state() is not None or saved not in (None,'fractional_live_test'):
                raise ValueError('모의/기존 전략 DB를 소수점 실거래 테스트에 재사용할 수 없습니다.')
            repo.set_control('runtime_kind','fractional_live_test')
            broker=FractionalLiveBroker(market,repo,armed=execute and acknowledge)
            broker.min_entry_to_exit_minutes=config['min_entry_to_exit_minutes']
            def persist_residual():
                if broker.failed: return
                try:
                    if broker.owned_quantities() or any(i['status'] not in TERMINAL for i in broker.orders.values()):
                        repo.record('alerts',{'id':'live-residual','timestamp':datetime.now(timezone.utc),
                            'severity':'critical','code':'live_residual_requires_review',
                            'message':'실거래 검증 종료 후 주문 또는 잔량이 남아 있습니다. 토스 앱에서 확인하세요.'})
                except Exception: pass
            stack.callback(persist_residual)
            if not await broker.reconcile(): raise CapabilityUnavailable('계좌 대사 실패: 주문을 차단했습니다.')
            stocks=await market.stocks([config['symbol']]); rows=stocks if isinstance(stocks,list) else stocks['stocks']
            stock=next((s for s in rows if s.get('symbol')==config['symbol']),{})
            if stock.get('currency')!='USD' or stock.get('status')!='ACTIVE' or stock.get('isCommonShare') is not True:
                raise CapabilityUnavailable('미국 상장 보통주 마스터 확인 실패')
            commissions=await market._get('/api/v1/commissions',account=True)
            now=datetime.now(timezone.utc); blockers=entry_blockers(config,now)
            if config['buy_amount']+Decimal('.10')>broker.cash: blockers.append('수수료 여유를 포함한 달러 현금 부족')
            if config['symbol'] in broker.state['baseline']: blockers.append('검증 종목을 이미 보유하고 있음')
            report={'mode':'live_preflight','live_orders_enabled':False,'account_seq':market.account_seq,
                'account_verified':True,'reconciled':True,'symbol':config['symbol'],'buy_amount':str(config['buy_amount']),
                'cash_buying_power':str(broker.cash),'buy_blockers':blockers,'managed_positions':{s:str(q) for s,q in broker.owned_quantities().items()},
                'commission_records':[{k:v for k,v in x.items() if k in {'marketCountry','commissionRate','startDate','endDate'}}
                    for x in commissions if x.get('marketCountry')=='US'],'actual_order_permissions_verified':False}
            repo.set_control('live_preflight',report)
            if not execute and not dashboard: return report
            # 복구 중 매도에는 신규 매수의 자격 만료를 적용하지 않는다.
            if not dashboard and not broker.orders and blockers: raise CapabilityUnavailable(' / '.join(blockers))
            fingerprint={k:str(config[k]) for k in ('symbol','buy_amount','max_buy_amount','strategy_cash','stop_pct','target_pct','daily_loss_pct')}
            previous=repo.get_control('live_test_settings')
            if previous is not None and previous!=fingerprint: raise ValueError('저장된 실거래 위험 설정과 현재 설정이 다릅니다.')
            repo.set_control('live_test_settings',fingerprint)
            from ..trading_controls import begin_session,pending,acknowledge as ack,heartbeat
            control=begin_session(repo,mode='live',account_seq=market.account_seq,now=datetime.now(timezone.utc),armed=execute and acknowledge)
            from ..trading_controls import finish_session
            stack.callback(finish_session,repo,control)
            manual_exits=set(broker.state.get('manual_exit_symbols',[]))
            if dashboard:
                from ..notifications import dashboard_environment
                app=Path(__file__).resolve().parents[1]/'dashboard'/'session_app.py'
                from ..session_config import load_session_config
                settings_file=Path(__file__).resolve().parents[2]/'config'/'sessionquant.yaml'
                usage_path=load_session_config(settings_file).db_path if settings_file.exists() else Path('data/sessionquant-paper-local.db')
                display=subprocess.Popen([sys.executable,'-m','streamlit','run',str(app),'--server.address','127.0.0.1',
                    '--server.port',str(port),'--server.headless','true','--','--db',str(config['db_path']),'--usage-db',str(usage_path),'--usage-environment','PAPER_LOCAL'],env=dashboard_environment(database=True))
                print(f'매매 제어 화면: http://127.0.0.1:{port} · 시작 대기',flush=True)
            notifier=DesktopNotifier()
            polls=0; need_exit=broker.state.get('system_exit_requested',False)
            while max_polls is None or polls<max_polls:
                polls+=1; now=datetime.now(timezone.utc)
                if not await broker.reconcile(): raise CapabilityUnavailable('실거래 대사 불일치. 자동 주문 중단.')
                from ..account_monitor import fetch_account_snapshot
                snapshot=await fetch_account_snapshot(market)
                repo.set_control('actual_account_snapshot',snapshot)
                repo.set_control('account_monitor_status',{'status':'ok','updated_at':snapshot['received_at']})
                stopped=False
                for request in pending(repo,control,now):
                    action=request['action']
                    if not execute:
                        ack(repo,request,ok=False,message='이번 실행은 실거래 권한이 없습니다.'); continue
                    if action=='stop_all':
                        control.update(running=False,draining=True); stopped=True; need_exit=True
                        broker.state['system_exit_requested']=True; broker._save()
                    elif action=='start':
                        current_blockers=entry_blockers(config,now)
                        if config['symbol'] in broker.positions or len(broker.positions)>=5: current_blockers.append('보유·노출 제한')
                        if config['buy_amount']+Decimal('.10')>broker.cash: current_blockers.append('현금 부족')
                        if stopped or control.get('completed') or current_blockers or (control['draining'] and (broker.owned_quantities() or any(i['status'] not in TERMINAL for i in broker.orders.values()))):
                            ack(repo,request,ok=False,message=' / '.join(current_blockers) or '종료·청산 중이거나 첫 소액 검증이 완료됐습니다.'); continue
                        control.update(running=True,draining=False)
                        need_exit=False
                        broker.state['system_exit_requested']=False; broker._save()
                    elif action=='sell_symbol':
                        bounds=session_bounds(now)
                        if not bounds or not bounds[0]<=now<bounds[1]-timedelta(hours=1):
                            ack(repo,request,ok=False,message='소수점 주문 접수 시간이 아닙니다.'); continue
                        symbol=request['symbol']
                        if symbol not in broker.positions:
                            ack(repo,request,ok=False,message='현재 보유 수량이 없습니다.'); continue
                        manual_exits.add(symbol)
                        broker.state['manual_exit_symbols']=sorted(manual_exits); broker._save()
                    ack(repo,request)
                if not control['running'] or control['draining']:
                    for cid,item in list(broker.orders.items()):
                        if item['request']['side']=='BUY' and item['status'] not in TERMINAL and execute:
                            await broker.cancel_order(cid)
                control['pending_manual_sells']=sorted(manual_exits)
                heartbeat(repo,control,now=datetime.now(timezone.utc),flat=not manual_exits and not broker.owned_quantities() and all(i['status'] in TERMINAL for i in broker.orders.values()))
                for cid,item in list(broker.orders.items()):
                    if item['request']['side']=='BUY' and item['request']['symbol'] in manual_exits and item['status'] not in TERMINAL and execute:
                        await broker.cancel_order(cid)
                control['pending_manual_sells']=sorted(manual_exits)
                if not execute:
                    await asyncio.sleep(config['poll_seconds']); continue
                for symbol in list(manual_exits):
                    owned=broker.owned_quantities().get(symbol,Decimal(0)); external=not bool(owned)
                    quantity=owned or broker.positions.get(symbol,Decimal(0))
                    active=any(i['request']['symbol']==symbol and i['status'] not in TERMINAL for i in broker.orders.values())
                    if not quantity and not active:
                        manual_exits.discard(symbol)
                        broker.state['manual_exit_symbols']=sorted(manual_exits); broker._save()
                    if quantity and not active and execute:
                        try:
                            await broker.submit_order(FractionalRequest('sqf_'+uuid4().hex,symbol,'SELL',quantity=quantity),
                                buy_cap=config['max_buy_amount'],external_position=external)
                        except (ValueError,CapabilityUnavailable):
                            repo.record('alerts',{'id':'manual-sell-'+symbol,'timestamp':now,'severity':'critical',
                                'message':symbol+' 매도 요청을 처리하지 못했습니다. 잔량·세션·미체결을 확인하세요.'})
                if not control['running'] and not broker.owned_quantities():
                    await asyncio.sleep(config['poll_seconds']); continue
                quote=await market.quote(config['symbol'],exchange=stock.get('market','UNKNOWN'),tradable=True)
                bid=decimal(quote.bid); ask=decimal(quote.ask)
                now=datetime.now(timezone.utc)
                if not quote.timestamp<=quote.received_at<=now: raise ValueError('호가 시각 오류')
                if any((now-t).total_seconds()>10 for t in (quote.timestamp,quote.received_at)) or quote.halted or quote.spread_pct>.003:
                    control['running']=False
                    repo.record('alerts',{'id':'stale-trading-quote','timestamp':now,'severity':'critical',
                        'message':'최신 호가를 확인하지 못해 신규 매수를 중단했습니다. 기존 잔량은 확인을 계속합니다.'})
                    heartbeat(repo,control,now=now)
                    await asyncio.sleep(config['poll_seconds']); continue
                if broker.failed: raise RuntimeError('DB 장애: 실제 주문 중단')
                state=net_state(broker,bid)
                repo.record('daily_performance',{'id':now.date().isoformat(),'timestamp':now,'execution_mode':'live',
                    'strategy_allocated_cash':str(config['strategy_cash']),'estimated_net_pnl':str(state['net_pnl']),
                    'fees_pending':state['fees_pending']})
                managed_buys=[i for i in broker.orders.values() if i['request']['side']=='BUY']
                if not managed_buys:
                    if not control['running'] or control['draining']:
                        await asyncio.sleep(config['poll_seconds']); continue
                    if entry_blockers(config,now): raise CapabilityUnavailable('매수 직전 자격 또는 세션 확인 실패')
                    if config['buy_amount']+Decimal('.10')>await broker.get_available_cash(): raise CapabilityUnavailable('현금 부족')
                    try:
                        await broker.submit_order(FractionalRequest('sqf_'+uuid4().hex,config['symbol'],'BUY',amount=config['buy_amount']),
                            buy_cap=config['max_buy_amount'],eligibility_verified=True)
                    except CapabilityUnavailable as exc:
                        control['running']=False
                        repo.record('alerts',{'id':'entry-blocked','timestamp':now,'severity':'warning','message':str(exc)})
                        heartbeat(repo,control,now=datetime.now(timezone.utc))
                else:
                    buys=[i for i in broker.orders.values() if i['request']['side']=='BUY']
                    first=utc(buys[0]['created_at']); bounds=session_bounds(now)
                    stop=bool(state['qty'] and (bid<=state['invested']/max(state['qty'],Decimal('.000001'))*(1-config['stop_pct'])
                        or state['net_pnl']<=-config['strategy_cash']*config['daily_loss_pct']))
                    profit=state['net_pnl']>=state['invested']*config['target_pct'] and state['invested']>0
                    due=not bounds or now>=bounds[1]-timedelta(hours=1,minutes=20)
                    need_exit=need_exit or control['draining'] or stop or profit or due or (now-first).total_seconds()>=config['max_hold_seconds']
                    if need_exit and not broker.state.get('system_exit_requested'):
                        broker.state['system_exit_requested']=True; broker._save()
                    if bounds and now>=bounds[1]-timedelta(hours=1,minutes=10) and (state['qty'] or any(i['status'] not in TERMINAL for i in broker.orders.values())):
                        alert={'id':'live-exit-overdue:'+now.date().isoformat(),'timestamp':now,'severity':'critical',
                            'code':'live_exit_incomplete','message':'실거래 당일 청산이 완료되지 않았습니다. 토스 앱의 미체결·잔량을 확인하세요.'}
                        repo.record('alerts',alert); notifier(alert)
                    for cid,item in list(broker.orders.items()):
                        if item['request']['side']=='BUY' and item['status'] not in TERMINAL and (need_exit or entry_blockers(config,now) or (now-first).total_seconds()>30):
                            await broker.cancel_order(cid)
                    # 매수 체결이 불명확한 동안 추가 매도를 제출하지 않는다.
                    buy_pending=any(i['request']['side']=='BUY' and i['status'] not in TERMINAL for i in broker.orders.values())
                    sell_pending=any(i['request']['side']=='SELL' and i['status'] not in TERMINAL for i in broker.orders.values())
                    if need_exit and state['qty'] and not buy_pending and not sell_pending:
                        if not bounds or now>=bounds[1]-timedelta(hours=1):
                            raise CapabilityUnavailable('소수점 접수 마감 이후 미청산 잔량. 토스 앱에서 확인하세요.')
                        await broker.submit_order(FractionalRequest('sqf_'+uuid4().hex,config['symbol'],'SELL',quantity=state['qty']),
                            buy_cap=config['max_buy_amount'])
                    if not state['qty'] and all(i['status'] in TERMINAL for i in broker.orders.values()):
                        control.update(running=False,completed=True)
                        heartbeat(repo,control,now=datetime.now(timezone.utc),flat=True)
                        if dashboard:
                            await asyncio.sleep(config['poll_seconds']); continue
                        return {**report,'mode':'live_test_finished','estimated_net_pnl':str(state['net_pnl']),
                            'order_statuses':{cid:i['status'] for cid,i in broker.orders.items()},
                            'actual_order_permissions_verified':any(decimal(i.get('filled_quantity','0'))>0 for i in broker.orders.values()),
                            'fees_pending':state['fees_pending'],'closed':True,'orders':len(broker.orders)}
                await asyncio.sleep(config['poll_seconds'])
            return {**report,'mode':'live_test_paused','actual_order_permissions_verified':any(decimal(i.get('filled_quantity','0'))>0 for i in broker.orders.values()),'closed':not bool(broker.owned_quantities()) and all(i['status'] in TERMINAL for i in broker.orders.values()),
                'message':'제한 횟수 종료. 실제 주문·잔량은 계좌와 원장에서 확인하세요.'}
    finally:
        if display:
            display.terminate()
            try: display.wait(timeout=5)
            except subprocess.TimeoutExpired: display.kill(); display.wait(timeout=5)
        if broker: broker.armed=False
        await market.aclose()
