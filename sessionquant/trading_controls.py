"""화면의 요청을 엔진에 전달한다. 화면은 브로커 API를 호출하지 않는다."""
from datetime import datetime,timezone
from uuid import uuid4
from .models import utc,symbol_valid

ACTIONS={'start','sell_symbol','stop_all'}
KEY='trade_control_state'


def begin_session(repo,*,mode,account_seq=None,now=None,armed=True):
    now=utc(now or datetime.now(timezone.utc))
    state={'run_id':uuid4().hex,'mode':mode,'account_seq':account_seq,'armed':armed,
        'running':False,'draining':False,'status':'WAITING','heartbeat':now.isoformat(),
        'started_at':now.isoformat(),'message':'매매 시작을 기다리고 있습니다.'}
    repo.set_control(KEY,state)
    return state


def enqueue(repo,action,*,run_id,account_seq=None,symbol=None,now=None,command_id=None):
    if action not in ACTIONS: raise ValueError('지원하지 않는 거래 제어 요청입니다.')
    if action=='sell_symbol': symbol_valid(symbol)
    now=utc(now or datetime.now(timezone.utc))
    state=repo.get_control(KEY,{})
    if state.get('run_id')!=run_id or state.get('account_seq')!=account_seq:
        raise ValueError('실행 중인 엔진과 계좌를 다시 확인하세요.')
    if not state.get('armed') or not 0<=(now-utc(state['heartbeat'])).total_seconds()<=60:
        raise ValueError('엔진이 연결되지 않았거나 거래 제어가 비활성화돼 있습니다.')
    cid=command_id or 'ui-'+uuid4().hex
    previous=repo.get_record('order_intents',cid)
    if previous: return previous
    value={'id':cid,'kind':'dashboard_command','action':action,'run_id':run_id,
        'account_seq':account_seq,'symbol':symbol,'requested_at':now.isoformat(),'status':'pending'}
    repo.record('order_intents',value)
    return value


def pending(repo,state,now):
    result=[]
    for item in repo.recent('order_intents',10000):
        if item.get('kind')!='dashboard_command' or item.get('status')!='pending': continue
        try:
            valid=(item.get('run_id')==state['run_id'] and item.get('account_seq')==state['account_seq']
                and item.get('action') in ACTIONS and 0<=(now-utc(item['requested_at'])).total_seconds()<=60)
        except (KeyError,TypeError,ValueError): valid=False
        if not valid:
            repo.record('order_intents',{**item,'status':'expired','message':'만료되었거나 이전 실행의 요청입니다.'})
        else: result.append(item)
    # 종료는 같은 갱신 주기의 시작보다 우선한다.
    return sorted(result,key=lambda r:r['action']!='stop_all')


def acknowledge(repo,item,*,ok=True,message='요청을 접수했습니다.'):
    repo.record('order_intents',{**item,'status':'accepted' if ok else 'rejected','message':message})


def heartbeat(repo,state,*,now,flat=False):
    if state['draining']:
        state['status']='STOPPED' if flat else 'LIQUIDATING'
    elif state.get('pending_manual_sells'): state['status']='LIQUIDATING'
    elif state.get('completed'): state['status']='STOPPED'
    elif state['running']: state['status']='RUNNING'
    else: state['status']='WAITING'
    state['heartbeat']=utc(now).isoformat()
    repo.set_control(KEY,state)


def render_controls(repo,*,positions,now=None):
    """확인 버튼에서만 명령을 저장하고 자동 갱신은 아무 주문도 생성하지 않는다."""
    import streamlit as st
    now=now or datetime.now(timezone.utc)
    state=repo.get_control(KEY,{})
    if not state: return
    st.subheader('매매 제어')
    st.caption('실제 주문 제어' if state['mode']=='live' else 'Paper 모의매매 제어')
    active=bool(state.get('armed')) and 0<=(now-utc(state['heartbeat'])).total_seconds()<=60
    labels={'WAITING':'시작 대기','RUNNING':'매매 중','LIQUIDATING':'청산 중','STOPPED':'매매 종료'}
    st.write('상태: '+labels.get(state.get('status'),'엔진 확인 필요'))
    if state.get('completed'): st.caption('이번 Phase 5 소액 검증의 1회 신규 매수가 완료되어 추가 진입은 제한됩니다.')
    if not active: st.warning('거래 엔진이 비활성화됐거나 연결이 끊어졌습니다. 버튼으로 실주문 권한을 활성화할 수 없습니다.')
    requests=repo.recent('order_intents',200)
    unresolved=[r for r in requests if r.get('kind')=='dashboard_command' and r.get('run_id')==state['run_id'] and r.get('status')=='pending']
    def submit(action,symbol=None):
        try:
            enqueue(repo,action,run_id=state['run_id'],account_seq=state.get('account_seq'),symbol=symbol)
            st.rerun()
        except (ValueError,RuntimeError): st.error('요청을 전달하지 못했습니다. 엔진과 DB 상태를 확인하세요.')
    busy=bool(unresolved)
    if st.button('매매 시작',disabled=not active or busy or state['running'] or state.get('completed') or (state['draining'] and state.get('status')!='STOPPED'),key='trading-start'):
        submit('start')
    with st.expander('전체 매매 종료'):
        st.caption('신규 매수 중단 → 미체결 매수 취소 → 이 시스템이 관리하는 포지션 전량 청산. 기존 외부 보유는 개별 매도 버튼으로 선택하세요.')
        confirm=st.checkbox('시스템 보유 포지션 전량 시장가 매도를 요청합니다.',key='confirm-stop')
        if st.button('전체 매매 종료 · 청산 요청',disabled=not active or any(r['action']=='stop_all' for r in unresolved) or not confirm,key='trading-stop'):
            submit('stop_all')
    for row in positions:
        symbol=row.get('symbol'); qty=row.get('qty',row.get('quantity'))
        if not symbol: continue
        with st.expander(f'{symbol} 시장가 매도 · 보유 {qty}'):
            supported=row.get('currency','USD')=='USD'
            confirm=st.checkbox(f'{symbol}의 표시된 보유 수량 전량을 시장가로 매도합니다.',key='confirm-sell-'+symbol)
            if not supported: st.caption('현재 실주문 제어는 미국 주식만 지원합니다.')
            if st.button('시장가 매도 요청',key='sell-'+symbol,disabled=not active or any(r.get('symbol')==symbol and r['action']=='sell_symbol' for r in unresolved) or not confirm or not supported):
                submit('sell_symbol',symbol)
    for row in [r for r in requests if r.get('kind')=='dashboard_command' and r.get('run_id')==state['run_id']][-5:]:
        st.caption(f"{row['action']} {row.get('symbol') or ''}: {row['status']} · {row.get('message','엔진 처리 대기')}")
    for alert in repo.recent('alerts',20)[-5:]:
        if alert.get('message'): st.warning(alert['message'])
    st.caption('청산 요청 접수는 체결 완료가 아닙니다. 실제 잔량·미체결 주문이 0인지 확인하세요.')


def finish_session(repo,state):
    try:
        state.update(armed=False,running=False,status='OFFLINE')
        repo.set_control(KEY,state)
    except Exception: pass
