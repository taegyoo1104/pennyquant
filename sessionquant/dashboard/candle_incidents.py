"""Display a bounded incident archive without querying full candle histories."""
import json


def render_incidents(archive):
    import streamlit as st
    from .session_app import timestamp
    from ..market.candle_incidents import MAX_EVENTS
    labels = {'CANDLE_LATE':'최신 확정봉 지연', 'CANDLE_REQUEST_DELAY':'분봉 요청 실패·대기', 'SLOW_CYCLE':'수집 순회 지연'}
    events = (archive or {}).get('events', [])[-MAX_EVENTS:]
    st.subheader('자동 보관한 분봉 지연 기록')
    st.caption('정상 수집은 추가 기록하지 않습니다. 전 종목 합산 5분에 최대 1회, 최근 96건·최대 256KiB만 보관합니다. 대표 종목은 최대 3개이며 모든 지연의 전체 이력은 아닙니다. 기록 없음이 정상 동작을 보장하지는 않습니다.')
    if not events:
        st.info('아직 자동 보관한 지연 기록이 없습니다.')
        return
    rows = []
    for event in reversed(events):
        phases = event.get('phases', {})
        rows.append({'기록 시각':timestamp(event.get('at')),
            '감지 내용':' · '.join(labels.get(r, '지연 확인') for r in event.get('reasons', [])),
            '대표 종목':', '.join(s['symbol'] for s in event.get('symbols', [])),
            '대상 수':event.get('affected_count'), '전체 시간(초)':event.get('cycle_seconds'),
            'API 응답 대기(초)':phases.get('api_http'), 'DB 요청(초)':phases.get('db_query'),
            '판단·저장(초)':phases.get('engine_batch')})
    st.dataframe(rows, hide_index=True, use_container_width=True)
    st.caption('시간은 서로 포함될 수 있어 합산하지 않습니다. 시각·처리 시간은 관측 기록이며 원인을 확정한 결과가 아닙니다. 파일에는 봉 시각과 마지막 판단 시각도 포함됩니다.')
    st.download_button('분봉 지연 진단 파일 받기', json.dumps(archive, ensure_ascii=False, indent=2),
        file_name='sessionquant-candle-delay.json', mime='application/json', key='candle-delay-download')
