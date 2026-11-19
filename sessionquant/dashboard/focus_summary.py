"""Small saved-price view; rendering never requests market data."""
from datetime import datetime, timezone, timedelta

from ..models import utc


def focus_rows(state, now=None, *, assessments=None):
    from .session_app import timestamp, usd
    now=now or datetime.now(timezone.utc)
    limit=state.get('data_max_age_seconds',120)
    try:current=state.get('status')=='VALID' and 0<=(now-utc(state['updated_at'])).total_seconds()<=limit
    except (ValueError,TypeError,KeyError,AttributeError):current=False
    result=[]
    show_progress=assessments is not None
    assessments={r['symbol']:r for r in (assessments or {}).get('rows',[])}
    for row in state.get('items',[]):
        if not row.get('focused'):continue
        at=row.get('last_price_at');price=row.get('last_price')
        try:fresh=price is not None and 0<=(now-utc(at)).total_seconds()<=limit
        except (ValueError,TypeError,AttributeError):fresh=False
        status='확인' if current and fresh else '시세 갱신 대기' if current else '마지막 기록 · 현재 집중 여부 미확인'
        if current and row.get('focus_refresh_pending'):status='자료 갱신 대기'
        item={'종목':row['ticker'],'최근 관측가(USD)':usd(price),'시세 시각':timestamp(at),'상태':status}
        if show_progress:
            saved=assessments.get(row['ticker'],{});p=saved.get('progress',{})
            try:
                boundary=now.replace(second=0,microsecond=0)
                active=(current and 0<=(now-utc(saved['evaluated_at'])).total_seconds()<=limit
                    and utc(p['five_at'])==boundary-timedelta(minutes=now.minute%5)
                    and utc(p['fifteen_at'])==boundary-timedelta(minutes=now.minute%15))
            except (KeyError,ValueError,TypeError,AttributeError):active=False
            order='매수 신호 없음'
            if saved.get('signal_present'):order='매수 신호 · 주문 조건 확인'
            if saved.get('order_status')=='BLOCKED':order='매수 보류'
            if saved.get('order_status')=='ALLOCATION_REJECTED':order='수량·보유 한도 등으로 보류'
            if saved.get('order_status')=='APPROVED':order='주문 승인 · 체결은 별도 확인'
            item.update({'눌림목 단계':p.get('path','판단 대기') if active else '판단 자료 갱신 대기',
                '5분봉 기준':timestamp(p.get('five_at')),'15분봉 기준':timestamp(p.get('fifteen_at')),
                '주문 판단':order if active else '마지막 평가 · 현재 판단 미확인'})
        result.append(item)
    return result


def render_focus_summary(state, assessments=None):
    import streamlit as st
    from .session_app import _table, timestamp
    st.markdown('**집중 감시 종목**')
    schedule=state.get('focus_schedule',{})
    if schedule:
        if schedule.get('status')=='BOUNDARY_DATA_WAIT':
            st.caption('재선정 시점의 확정봉 확인 중 · 대기 한도 '+timestamp(schedule.get('pending_until')))
        elif schedule.get('next_at'):
            st.caption('개장 기준 30분마다 재선정 · 다음 '+timestamp(schedule['next_at'])+' · 유효한 조정·반등 패턴은 유지')
    _table(focus_rows(state,assessments=assessments),'집중 종목 선정·시세 수신을 기다립니다.')
    if assessments:
        st.caption('단계는 최신 확정봉 배열의 현재 평가입니다. 한 번 통과한 조건을 고정하지 않으며 상승 조건이 깨지면 대기합니다. 봉 기준 시각은 마감 시각입니다.')
