"""Explain saved news preparation without claiming that a stale worker is online."""
from datetime import datetime, timezone

from ..models import utc
from ..news.analysis_queue import session_phase


def queue_summary(status, now=None):
    queue = (status or {}).get('analysis_queue') or {}
    if not queue:
        return None
    now = utc(now or datetime.now(timezone.utc))
    phase = session_phase(now)
    try:
        age = (now - utc(queue['updated_at'])).total_seconds()
        fresh = 0 <= age <= 900
    except (ValueError, TypeError, KeyError):
        fresh = False
    titles = {'PREMARKET': '장전 뉴스 준비', 'REGULAR': '뉴스 순차 분석', 'CLOSED': '정규장 분석 대기'}
    counts = queue.get('counts', {})
    details = {'PREMARKET': '장전에는 수집·저장·중복 정리만 하고, AI 분석은 개장 후 시작합니다.',
               'REGULAR': '보유·집중 종목과 최신 뉴스를 우선해 최소 5분 간격으로 최대 3건씩 분석합니다.',
               'CLOSED': '정규장 밖에서는 새 AI 분석을 요청하지 않습니다.'}
    return {'title': titles[phase] if fresh else '마지막 뉴스 준비 기록',
            'detail': details[phase], 'pending': queue.get('pending', 0),
            'received': counts.get('received', 0), 'duplicates': counts.get('duplicates', 0),
            'excluded': sum(counts.get(k, 0) for k in ('expired', 'excluded', 'capacity_excluded')),
            'day': queue.get('day'), 'updated_at': queue.get('updated_at'), 'fresh': fresh}


def render_news_queue(status):
    import streamlit as st
    from .session_app import timestamp
    summary = queue_summary(status)
    if not summary:
        return
    st.info(f"{summary['title']} · 분석 대기 {summary['pending']}건\n\n{summary['detail']}")
    st.caption(f"미국 날짜 {summary['day']} · 분석 후보 수신 {summary['received']}건 · "
               f"중복 제외 {summary['duplicates']}건 · 기간·검증·대기 한도 제외 {summary['excluded']}건")
    st.caption('기사·종목별 분석 후보 기준 · 마지막 확인 '+timestamp(summary['updated_at'])+
               ' · 같은 본문은 묶고 내용이 바뀐 기사는 별도로 남깁니다.')
