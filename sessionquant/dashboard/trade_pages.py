"""Small trade-history pages without changing the ledger or CSV export."""
from datetime import datetime

PAGE_SIZE = 15
MAX_PAGES = 5


def newest_first(rows):
    def stamp(row):
        try:
            value = datetime.fromisoformat(str(row.get('timestamp') or row.get('created_at')).replace('Z', '+00:00'))
            return value.timestamp() if value.tzinfo else float('-inf')
        except (ValueError, TypeError, OverflowError):
            return float('-inf')
    return sorted(rows, key=stamp, reverse=True)


def trade_page(rows, *, kind, source_count, scope="detail"):
    import streamlit as st
    label = '주문' if kind == 'orders' else '체결'
    if not rows:
        st.info(f'선택한 기간·종목·전략에 맞는 {label} 기록이 없습니다. 필터를 변경해 주세요.'
                if source_count else f'현재 모의 장부에서 조회된 {label} 기록이 없습니다.')
        return []
    visible = newest_first(rows)[:PAGE_SIZE * MAX_PAGES]
    pages = (len(visible) + PAGE_SIZE - 1) // PAGE_SIZE
    key = f'trade_{scope}_{kind}_page'
    filters = tuple(st.session_state.get(name) for name in ('trade_symbol', 'trade_strategy', 'trade_period_v2')) if scope == 'detail' else ()
    filter_key = key + '_filters'
    if st.session_state.get(filter_key) != filters or not 1 <= st.session_state.get(key, 1) <= pages:
        st.session_state[key] = 1
    st.session_state[filter_key] = filters
    page = st.selectbox(f'{label} 페이지', range(1, pages + 1), key=key,
                        format_func=lambda n: f'{n} / {pages} 페이지')
    start = (page - 1) * PAGE_SIZE
    st.caption(f'{label} {len(rows):,}건 중 {start + 1}–{min(start + PAGE_SIZE, len(visible))}번째 · 최신순 · 페이지당 15건')
    if len(rows) > len(visible):
        st.caption('화면에는 최근 75건까지 표시합니다. 종목·전략 필터로 범위를 좁힐 수 있으며, 체결 CSV는 페이지와 관계없이 필터에 맞는 조회 기록을 포함합니다.')
    return visible[start:start + PAGE_SIZE]
