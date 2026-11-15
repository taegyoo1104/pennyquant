"""기존 시장 탭에 거래 후보와 구분된 참고 ETF를 표시한다."""
from ..market_reference.contracts import REFERENCES


def reference_rows(view, now=None):
    from datetime import datetime, timezone
    from ..models import utc
    from .session_app import timestamp, usd
    now = now or datetime.now(timezone.utc)
    rows = []
    for symbol, (_, label) in REFERENCES.items():
        item = (view.get('items') or {}).get(symbol) or {}
        windows = item.get('windows') or {}
        quality = item.get('quality_status', 'UNKNOWN')
        try:
            if quality == 'GOOD' and now > utc(item['valid_until']): quality = 'STALE'
        except (ValueError, KeyError, TypeError, AttributeError): quality = 'UNKNOWN'
        valid=quality=='GOOD'
        latest=(item.get('latest_observations') or {}).get('price') or (item.get('latest_observations') or {}).get('candle') or {}
        rows.append({'종목': symbol, '의미': label, '용도': '시장 참고 전용',
            '가격': usd(item.get('current_price')) if valid else '—', '지연 구분': 'STALE' if quality=='STALE' else item.get('latency_class','UNKNOWN_LATENCY'),
            '공급원':latest.get('source','미확인'),'수집 시각':timestamp(latest.get('received_at')),
            '전일 대비(%)':item.get('day_return_pct') if valid and item.get('cross_day_comparability')=='VERIFIED' else None,
            '5분 ETF 등락률(%)':(windows.get('5') or {}).get('pct_change') if valid and (windows.get('5') or {}).get('quality_status')=='GOOD' else None,
            '품질': quality, '15분 ETF 등락률(%)': (windows.get('15') or {}).get('pct_change') if valid and (windows.get('15') or {}).get('quality_status')=='GOOD' else None,
            '30분 ETF 등락률(%)': (windows.get('30') or {}).get('pct_change') if valid and (windows.get('30') or {}).get('quality_status')=='GOOD' else None,
            '원본 시각': timestamp(item.get('event_at')), '확인 가능 시각': timestamp(item.get('available_at')),
            '유효 시각': timestamp(item.get('valid_until')), '근거': str(item.get('quality_reasons') or [])})
    return rows


def render_references(view, risk=None, fred=None):
    import streamlit as st
    st.subheader('시장 참고 ETF · 거래 후보 제외')
    st.caption('ETF 가격 변화입니다. 금리 bp 또는 WTI 현물 등락률로 환산하지 않습니다. 조회 성공과 실시간 권한은 별도로 검증합니다.')
    st.dataframe(reference_rows(view), use_container_width=True, hide_index=True)
    st.caption('SHY·IEF·TLT·USO·SPY·QQQ는 감시 100·집중 20 정원에서 제외됩니다. 권한·가격 비교 가능성이 미확인되면 UNKNOWN으로 표시합니다.')
    if risk:
        from .session_app import timestamp
        st.caption(f"참고 위험 관찰: {risk.get('status','UNKNOWN')} · 평가 시각: {timestamp(risk.get('observed_at'))} · 가설: {risk.get('threshold_version','UNKNOWN')}")
        st.caption('저장된 과거 위험 평가입니다. 현재 단계는 관찰 전용이며 매매에 적용하지 않습니다. UNKNOWN은 안전하다는 뜻이 아닙니다.')
    st.caption('FRED 공식 일별 기준 · 장중 ETF와 수집 주기·단위가 다릅니다.')
    if fred:
        from .session_app import timestamp
        latest = {}
        for row in fred: latest[row.get('series')] = row
        st.dataframe([{'지표': r.get('series'), '값': r.get('value'), '단위': r.get('units'),
            '관측일': r.get('observation_date'), '실제 발표 시각': timestamp(r.get('published_at')),
            '수신 시각': timestamp(r.get('received_at')), '조회 기준일': r.get('requested_asof'),
            '자료 구분': '공식 일별 · 실제 발표/수정 시각 미확인'} for r in latest.values()],
            use_container_width=True, hide_index=True)
    else:
        st.caption('저장된 FRED 일별 참고 자료가 없습니다. 값을 추정하지 않습니다.')
