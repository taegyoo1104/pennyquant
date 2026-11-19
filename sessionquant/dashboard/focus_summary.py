"""Small saved-price view; rendering never requests market data."""
from datetime import datetime, timezone

from ..models import utc


def focus_rows(state, now=None):
    from .session_app import timestamp, usd
    now=now or datetime.now(timezone.utc)
    limit=state.get('data_max_age_seconds',120)
    try:current=state.get('status')=='VALID' and 0<=(now-utc(state['updated_at'])).total_seconds()<=limit
    except (ValueError,TypeError,KeyError,AttributeError):current=False
    result=[]
    for row in state.get('items',[]):
        if not row.get('focused'):continue
        at=row.get('last_price_at');price=row.get('last_price')
        try:fresh=price is not None and 0<=(now-utc(at)).total_seconds()<=limit
        except (ValueError,TypeError,AttributeError):fresh=False
        status='확인' if current and fresh else '시세 갱신 대기' if current else '마지막 기록 · 현재 집중 여부 미확인'
        if current and row.get('focus_refresh_pending'):status='자료 갱신 대기'
        result.append({'종목':row['ticker'],'최근 관측가(USD)':usd(price),'시세 시각':timestamp(at),'상태':status})
    return result


def render_focus_summary(state):
    import streamlit as st
    from .session_app import _table
    st.markdown('**집중 감시 종목**')
    _table(focus_rows(state),'집중 종목 선정·시세 수신을 기다립니다.')
