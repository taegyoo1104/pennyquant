"""Ordinary waiting-reason tables and their CSV exports use readable labels."""
import pytest
from streamlit.testing.v1 import AppTest


@pytest.mark.parametrize('raw', [False, True])
def test_reason_table_exports_codes_only_when_diagnostics_enabled(raw):
    frame = {'rows': [{'symbol': 'NVDA', 'blockers': [
        'technical_history_incomplete', 'technical_signal_absent', 'new_provider_rule'],
        'indicators': {}}]}
    app = AppTest.from_string(
        'import streamlit as st\n'
        'from sessionquant.dashboard.selection_panel import render_selection\n'
        f'st.session_state["dashboard_raw_data"] = {raw!r}\n'
        f'render_selection({frame!r})\n').run()
    assert not app.exception
    table = next(x.value for x in app.dataframe if '대기 이유' in x.value.columns)
    assert table['대기 이유'].tolist() == [
        '매매 지표를 계산할 당일 분봉 부족', '전략 매수 신호 없음', '미등록 제한']
    exported = table.to_csv(index=False)
    for code in frame['rows'][0]['blockers']:
        assert (code in exported) is raw
    assert ('사유 코드' in table.columns) is raw
