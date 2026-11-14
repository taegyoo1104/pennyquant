"""기존 시장 탭에 거래 후보와 구분된 참고 ETF를 표시한다."""
from ..market_reference.contracts import REFERENCES


def reference_rows(view):
    from .session_app import timestamp, usd
    rows = []
    for symbol, (_, label) in REFERENCES.items():
        item = (view.get('items') or {}).get(symbol) or {}
        windows = item.get('windows') or {}
        rows.append({'종목': symbol, '의미': label, '용도': '시장 참고 전용',
            '가격': usd(item.get('current_price')), '지연 구분': item.get('latency_class', 'UNKNOWN'),
            '품질': item.get('quality_status', 'UNKNOWN'), '15분 ETF 등락률(%)': (windows.get('15') or {}).get('pct_change'),
            '30분 ETF 등락률(%)': (windows.get('30') or {}).get('pct_change'),
            '원본 시각': timestamp(item.get('event_at')), '확인 가능 시각': timestamp(item.get('available_at')),
            '유효 시각': timestamp(item.get('valid_until')), '근거': str(item.get('quality_reasons') or [])})
    return rows


def render_references(view):
    import streamlit as st
    st.subheader('시장 참고 ETF · 거래 후보 제외')
    st.caption('ETF 가격 변화입니다. 금리 bp 또는 WTI 현물 등락률로 환산하지 않습니다. 조회 성공과 실시간 권한은 별도로 검증합니다.')
    st.dataframe(reference_rows(view), use_container_width=True, hide_index=True)
    st.caption('SHY·IEF·TLT·USO·SPY·QQQ는 감시 100·집중 20 정원에서 제외됩니다. 권한·가격 비교 가능성이 미확인되면 UNKNOWN으로 표시합니다.')
