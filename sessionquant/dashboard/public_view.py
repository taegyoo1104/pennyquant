"""Public allowlisted home. Never dispatch user-supplied page/panel or raw diagnostic downloads."""
from pathlib import Path
import streamlit as st

from .session_app import load_database, build_view


@st.cache_data(ttl=10, max_entries=2, show_spinner=False)
def snapshot(path):
    # One shared DB read per 10s, rather than a connection/scan for every visitor.
    return load_database(Path(path), page='대시보드',panel='public')


def render_public(path):
    from .overview import style
    from .operations_overview import render_operations
    style()
    # No raw data option, system page, query parameter navigation, or downloads.
    st.session_state['dashboard_raw_data'] = False
    @st.fragment(run_every=15)
    def home():
        try:
            data = snapshot(str(path))
            if data.error:
                st.info('저장된 화면을 준비하고 있습니다. 잠시 후 자동으로 다시 확인합니다.')
                return
            view = build_view(data)
            if view['environment'] != 'PAPER_LOCAL':
                st.info('공개 모의매매 화면을 준비하고 있습니다.')
                return
            st.caption('저장된 운영 기록 · 약 15초마다 화면 갱신 · 휴장·폐장에는 마지막 거래일 기록')
            render_operations(data, view, path)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('public_dashboard_read_failed')
            st.info('화면을 준비하고 있습니다. 잠시 후 다시 확인해 주세요.')
    home()
