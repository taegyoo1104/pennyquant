"""메인 화면의 실제 계좌와 OpenAI 비용을 출처·통화별로 구분한다."""
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
from dataclasses import replace
from ..portfolio.models import decimal


def open_cost_repository(path,*,environment=None):
    from ..database.config import database_backend,MySQLConfig
    from ..database.repository import Repository
    if database_backend()=='mysql' and environment:
        from ..database.mysql_repository import MySQLRepository
        return MySQLRepository(path,replace(MySQLConfig.from_env(),environment=environment,namespace=None))
    if database_backend()=='sqlite' and not Path(path).exists(): return None
    return Repository(path)


def cost_summary(repo,now=None):
    now=now or datetime.now(timezone.utc)
    months=repo.get_control('session_llm_budget_v1',{})
    spent=sum((decimal(row.get('spent','0'),nonnegative=True) for row in months.values()),Decimal(0)) if months else None
    current=months.get(now.strftime('%Y-%m'))
    month=decimal(current.get('spent','0')) if current else (Decimal(0) if months else None)
    funding=repo.get_control('openai_funding_note',{})
    total=decimal(funding['total_usd'],nonnegative=True) if funding.get('total_usd') is not None else None
    return {'spent':spent,'month':month,'deposited':total,'funding_updated_at':funding.get('updated_at'),
        'health':repo.get_control('llm_provider_health',{}),'usage_known':bool(months)}


def render_cost_panel(path,*,usage_path=None,usage_environment=None):
    import streamlit as st
    from .session_app import usd
    repo=None
    st.subheader('OpenAI 사용량·충전 기록')
    try:
        repo=open_cost_repository(usage_path or path,environment=usage_environment)
        if repo is None:
            st.caption('분석 비용 원장이 아직 없습니다. 사용량을 0으로 확정하지 않습니다.')
            return
        summary=cost_summary(repo)
        cols=st.columns(3)
        cols[0].metric('프로그램 누적 비용 · 추정',usd(summary['spent']))
        cols[1].metric('이번 달 프로그램 비용 · 추정',usd(summary['month']))
        cols[2].metric('OpenAI 충전 누계 · 직접 기록',usd(summary['deposited']))
        st.caption('선택한 운영 DB의 토큰 단가 기반 비용입니다. 다른 앱·조직 전체 청구액·공식 현재 잔액과 다릅니다. 충전 미입력이나 $0 기록으로 매매를 차단하지 않습니다.')
        health=summary['health']
        if health.get('code') not in {None,'UNKNOWN','AVAILABLE'}:
            st.warning(health.get('message','LLM 미사용: 차트 기반 매매·위험 관리 계속'))
        elif not health or health.get('code')=='UNKNOWN':
            st.caption('LLM 생성·잔액 상태는 미확인입니다. 키 정보 조회 성공만으로 잔액이 있다고 판단하지 않습니다.')
        with st.expander('OpenAI 충전 누계 기록'):
            value=st.text_input('OpenAI에 충전한 총 달러 금액',value=str(summary['deposited']) if summary['deposited'] is not None else '',key='openai-deposit')
            if st.button('충전 누계 저장',key='save-openai-deposit'):
                try:
                    amount=decimal(value,nonnegative=True)
                    repo.set_control('openai_funding_note',{'total_usd':str(amount),'updated_at':datetime.now(timezone.utc).isoformat(),'source':'user_entered'})
                    st.success('직접 입력한 누계를 저장했습니다. 실제 충전·자동 잔액 조회를 수행한 것은 아닙니다.')
                    st.rerun()
                except ValueError: st.error('0 이상의 달러 금액을 입력하세요.')
            st.markdown('[OpenAI 공식 사용량](https://platform.openai.com/usage) · [충전·잔액 확인](https://platform.openai.com/settings/organization/billing/overview)')
    except Exception:
        st.warning('OpenAI 비용 기록을 읽지 못했습니다. 이 표시 오류로 매매를 중단하지 않습니다.')
    finally:
        if repo: repo.close()


def render_actual_account(snapshot,status,*,show_metrics=True):
    import streamlit as st
    from .account_app import money
    from .session_app import timestamp,_table
    st.subheader('토스 실제 계좌')
    if not snapshot:
        st.caption('실제 계좌 연결을 기다리고 있습니다. 모의 자금은 실제 잔고가 아닙니다.')
        return
    st.caption('실제 계좌 마지막 조회: '+timestamp(snapshot['received_at']))
    if status.get('status')=='error': st.warning(status.get('message','실계좌 갱신 실패: 이전 기록입니다.'))
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(snapshot['received_at'])).total_seconds()
    if age>60: st.warning('실계좌 기록이 60초 이상 지연됐습니다.')
    for issue in snapshot.get('issues',[]): st.warning(issue)
    market=((snapshot.get('summary') or {}).get('marketValue') or {}).get('amount') or {}
    cash=snapshot.get('buying_power') or {}
    if show_metrics:
        cols=st.columns(4)
        for col,label,value,currency in [(cols[0],'미국 주식 평가금액',market.get('usd'),'USD'),(cols[1],'달러 매수 가능 금액',cash.get('USD'),'USD'),
            (cols[2],'국내 주식 평가금액',market.get('krw'),'KRW'),(cols[3],'원화 매수 가능 금액',cash.get('KRW'),'KRW')]: col.metric(label,money(value,currency))
    _table([{'종목':r['symbol'],'이름':r.get('name'),'통화':r['currency'],'보유 수량':r['quantity'],
        '평균 매수가':money(r.get('averagePurchasePrice'),r['currency']),
        '현재가':money(r.get('lastPrice'),r['currency']),
        '평가금액':money((r.get('marketValue') or {}).get('amount'),r['currency']),
        '평가손익':money((r.get('profitLoss') or {}).get('amount'),r['currency'])} for r in snapshot['items']], '조회된 계좌에 보유 주식이 없습니다.')
    st.caption('매수 가능 금액은 예수금·총자산과 다를 수 있습니다. 원화·달러를 합산하거나 누락 값을 0으로 만들지 않습니다.')

    with st.expander('실계좌 미체결 주문'):
        orders=snapshot.get('open_orders')
        if orders is None: st.info('미체결 목록은 조회하지 못했습니다.')
        else:
            _table([{'종목':o.get('symbol'),'방향':o.get('side'),'상태':o.get('status'),'통화':o.get('currency'),
                '주문 수량':o.get('quantity'),'금액 주문':o.get('orderAmount')} for o in orders],
                'API에서 조회 가능한 미체결 주문이 없습니다.')
