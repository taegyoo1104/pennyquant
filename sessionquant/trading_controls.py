"""화면의 요청을 엔진에 전달한다. 화면은 브로커 API를 호출하지 않는다."""
from datetime import datetime,timezone
from uuid import uuid4
from .models import utc,symbol_valid

ACTIONS={'start','sell_symbol','stop_all'}
KEY='trade_control_state'


def can_start(state):
    """Paper can resume after the engine reports a completed operator liquidation."""
    if state.get('data_ready') is False or state.get('completed'):
        return False
    if state.get('status')=='WAITING' and not state.get('draining'):
        return True
    return (state.get('mode')=='paper' and state.get('status')=='STOPPED'
            and state.get('draining') is True and not state.get('running'))


def begin_session(repo,*,mode,account_seq=None,now=None,armed=True):
    now=utc(now or datetime.now(timezone.utc))
    state={'run_id':uuid4().hex,'mode':mode,'account_seq':account_seq,'armed':armed,
        'running':False,'draining':False,'status':'WAITING','heartbeat':now.isoformat(),
        'started_at':now.isoformat(),'message':'매매 시작을 기다리고 있습니다.'}
    repo.set_control(KEY,state)
    return state


def enqueue(repo,action,*,run_id,account_seq=None,symbol=None,now=None,command_id=None):
    from .dashboard_mode import require_writable_dashboard
    require_writable_dashboard()
    from .dashboard.access import protected
    if action == 'start' and protected():
        from .market.bars import session_bounds
        clock = utc(now or datetime.now(timezone.utc))
        bounds = session_bounds(clock)
        if not bounds or not bounds[0] <= clock < bounds[1]:
            raise ValueError('정규장 운영 시간이 아닙니다. 매매를 시작할 수 없습니다.')
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
    value['requested_by'] = 'dashboard_admin' if protected() else 'local_operator'
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
    elif state.get('data_ready') is False: state['status']='WARMING'
    elif state['running']: state['status']='RUNNING'
    else: state['status']='WAITING'
    state['heartbeat']=utc(now).isoformat()
    repo.set_control(KEY,state)


def request_command(repo,action,*,run_id,environment,symbol=None,now=None):
    """동일 제어 행을 잠가 복수 화면의 요청을 직렬화하고 현재 실행을 재검증한다."""
    from .dashboard_mode import require_writable_dashboard
    require_writable_dashboard()
    from hashlib import sha256
    now=utc(now or datetime.now(timezone.utc))
    result=[]
    def locked(state):
        mode='live' if environment=='LIVE_LOCAL' else 'paper' if environment=='PAPER_LOCAL' else None
        if mode is None or state.get('mode')!=mode or state.get('run_id')!=run_id:
            raise ValueError('화면과 엔진 실행 환경이 다릅니다.')
        if not state.get('armed') or not 0<=(now-utc(state['heartbeat'])).total_seconds()<=60:
            raise ValueError('엔진 연결이 만료됐습니다.')
        if action=='start' and not can_start(state):
            raise ValueError('현재 엔진 상태에서 시작할 수 없습니다.')
        for row in repo.recent('order_intents',10000):
            if (row.get('kind')=='dashboard_command' and row.get('run_id')==run_id
                and row.get('status')=='pending' and row.get('action')==action and row.get('symbol')==symbol):
                result.append(row);return state
        if action=='sell_symbol':
            from .portfolio.models import decimal
            ledger=repo.load_state() or {}
            position=(ledger.get('broker',{}).get('positions') or {}).get(symbol,{})
            if mode!='paper' or decimal(position.get('qty','0'))<=0:
                raise ValueError('전략 관리 수량을 검증할 수 없습니다.')
            if symbol in (ledger.get('broker',{}).get('exit_intents') or {}):
                raise ValueError('이미 매도 처리 중입니다. 남은 수량을 확인하세요.')
            orders=repo.recent('orders',10000)
            if any(r.get('symbol')==symbol and str(r.get('side')).lower()=='sell' and str(r.get('status')).lower() not in {'filled','cancelled','rejected'} for r in orders):
                raise ValueError('매도 주문이 진행 중입니다.')
        cid='ui-'+sha256(f"{run_id}:{state.get('heartbeat')}:{action}:{symbol}".encode()).hexdigest()
        result.append(enqueue(repo,action,run_id=run_id,account_seq=state.get('account_seq'),symbol=symbol,now=now,command_id=cid))
        return state
    repo.update_control(KEY,locked,{})
    return result[0]


def render_controls(repo,*,positions,now=None,environment=None,readonly=False,compact=False,ledger_state=None):
    """실행별 확인 상태를 보존하고 최종 버튼 클릭에서만 기존 큐를 사용한다."""
    import streamlit as st
    from .dashboard.access import is_admin
    if not is_admin():
        st.caption('공개 조회 · 매매 조작은 관리자만 할 수 있습니다.')
        return
    from .dashboard_mode import inspection_mode, inspection_start_message
    if inspection_mode():
        st.markdown('**자동매매 꺼짐 · 기록 조회 중**')
        st.caption(inspection_start_message(now))
        if st.button('매매 시작', key='trading-start'):
            st.info(inspection_start_message(now))
        st.button('일괄 매매 종료·청산', disabled=True, key='trading-stop')
        return
    now=utc(now or datetime.now(timezone.utc));state=repo.get_control(KEY,{})
    if not compact:st.subheader('실행 상태')
    if not state:
        st.caption('프로그램 연결 확인 필요')
        if compact:
            st.button('매매 시작',disabled=True,key='trading-start')
            st.button('일괄 매매 종료·청산',disabled=True,key='trading-stop')
        return
    environment=environment or ('LIVE_LOCAL' if state.get('mode')=='live' else 'PAPER_LOCAL')
    try:active=state.get('armed') and 0<=(now-utc(state['heartbeat'])).total_seconds()<=60
    except (KeyError,ValueError,TypeError):active=False
    expected='live' if environment=='LIVE_LOCAL' else 'paper' if environment=='PAPER_LOCAL' else None
    active=bool(active and expected==state.get('mode') and not readonly)
    from .dashboard.labels import label
    status=label(state.get('status','UNKNOWN')) if active else '연결 확인 필요 · 조회만 가능'
    if compact:
        if active:status={'WAITING':'자동매매 꺼짐','RUNNING':'자동매매 켜짐'}.get(state.get('status'),status)
        columns=st.columns([2,1,1])
        columns[0].markdown('**'+status+'**')
        if state.get('status')=='WAITING' and active:columns[0].caption('매매 시작 버튼 대기')
        if state.get('status')=='RUNNING' and active:columns[0].caption('조건이 맞을 때만 모의 주문을 냅니다.')
    else:st.write('**실행 상태 · '+status+'**')
    run=state['run_id']
    requests=[r for r in repo.recent('order_intents',200) if r.get('kind')=='dashboard_command' and r.get('run_id')==run]
    # Reuse the display snapshot; request_command still reads the authoritative
    # ledger again when a sell is actually submitted.
    ledger = ledger_state if ledger_state is not None else repo.load_state() or {}
    selling=set((ledger.get('broker') or {}).get('exit_intents') or {})
    unresolved=[r for r in requests if r.get('status')=='pending']
    def submit(action,symbol=None):
        try:
            request_command(repo,action,run_id=run,environment=environment,symbol=symbol,now=now)
            st.rerun()
        except Exception:st.error('요청을 전달하지 못했습니다. 현재 실행·관리 수량·미체결을 확인하세요.')
    resume=state.get('mode')=='paper' and state.get('status')=='STOPPED' and state.get('draining') is True
    start_label='매매 재개' if resume else '매매 시작'
    with columns[1].popover(start_label,width='stretch',type='primary') if compact else st.expander(start_label+' 확인'):
        if resume:st.caption('청산이 끝난 모의매매를 재개합니다. 기존 자금·손익·거래 기록과 위험 제한은 유지됩니다.')
        confirm=st.checkbox('표시된 계좌와 모드를 확인했습니다.',key=('confirm-resume-' if resume else 'confirm-start-')+run)
        if st.button(start_label,disabled=not active or bool(unresolved) or not can_start(state) or not confirm,key='trading-start'):submit('start')
    with columns[2].popover('전체 매도',width='stretch') if compact else st.expander('일괄 매매 종료·청산 확인'):
        st.caption('신규 매수 차단 → 미체결 매수 취소 → 전략 관리 포지션 시장가 청산. 시장 시간·유동성에 따라 부분체결/미체결될 수 있습니다.')
        if expected=='paper':st.caption('보유 수량과 미체결 주문이 모두 정리되면 매매 재개를 누를 수 있습니다.')
        st.caption('대상: '+(', '.join(str(r.get('symbol'))+' '+str(r.get('qty'))+'주' for r in positions) or '관리 포지션 없음/미확인'))
        confirm=st.checkbox('시스템 관리 포지션 전량 청산 위험을 확인했습니다.',key='confirm-stop-'+run)
        live_ok=st.checkbox('마스킹된 실제 계좌 **의 청산 요청을 추가 확인합니다.',key='confirm-live-'+run) if expected=='live' else True
        if st.button('일괄 매매 종료·청산 요청',disabled=not active or not confirm or not live_ok or any(r.get('action')=='stop_all' for r in unresolved),key='trading-stop'):submit('stop_all')
    from .portfolio.models import decimal
    from .dashboard.instrument_names import korean_name
    for row in [] if compact else positions:
        symbol=row.get('symbol');qty=row.get('qty')
        if not symbol:continue
        try:positive=decimal(qty)>0
        except (ValueError,TypeError):positive=False
        name = korean_name(symbol, row)
        display_name = f'{name}({symbol})' if name else symbol
        sell_label = f'{display_name} {qty}주 전량 매도'
        with st.expander(sell_label):
            if expected=='paper':st.caption('주 단위의 모의 보유 수량입니다. 금액에 맞춰 매수하므로 1주보다 적을 수 있어요.')
            st.caption(label(environment)+' · 시장가 위험/미체결 수량은 엔진에서 다시 확인합니다.')
            confirm=st.checkbox(f'{sell_label}를 확인합니다.',key='confirm-sell-'+symbol+'-'+run)
            if st.button(sell_label,key='sell-'+symbol,disabled=not active or expected=='live' or not positive or not confirm or symbol in selling or any(r.get('symbol')==symbol and r.get('status')=='pending' for r in requests)):
                submit('sell_symbol',symbol)
    if not compact:
        st.caption('주문 처리와 남은 수량은 매매 내역에서 확인하세요.')
        for row in requests[-5:]:st.caption(str(row.get('action'))+': '+str(row.get('status')))


def finish_session(repo,state):
    try:
        state.update(armed=False,running=False,status='OFFLINE')
        repo.set_control(KEY,state)
    except Exception: pass
