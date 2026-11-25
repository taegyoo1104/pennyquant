"""Maintenance progress UI: no DB connection, provider keys or write actions."""
import json
from pathlib import Path
import sys


def load_progress(root):
    path = Path(root) / 'progress.json'
    try:
        if path.stat().st_size > 131072:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def render():
    import streamlit as st
    st.set_page_config(page_title='SessionQuant · 자료 정리', layout='centered')
    st.title('SESSIONQUANT')
    st.info('수집·매매·AI 분석을 멈추고 자료를 정리하고 있습니다. VM과 DB는 켜져 있습니다.')
    @st.fragment(run_every=10)
    def progress():
        p = load_progress('/app/maintenance')
        state = p.get('state')
        messages = {'RUNNING': '오래된 자료 정리 중', 'COMPLETE': '정리 완료 · 최종 백업 및 화면 전환 확인 중',
                    'DISABLED': '자료 정리 비활성', 'PARTIAL': '정리 미완료 · 저장된 진행 상황 확인 필요',
                    'FAILED': '정리 중단 · 오류 확인 필요'}
        st.subheader(messages.get(state, '백업·자료 정리 준비 중'))
        cols = st.columns(2)
        for col, label, key in zip(cols, ('확인한 기록', '삭제한 기록'), ('scanned', 'deleted')):
            value = p.get(key)
            col.metric(label, f'{value:,}건' if type(value) is int and value >= 0 else '확인 중')
        details = p.get('maintenance')
        seconds = details.get('elapsed_seconds') if isinstance(details, dict) else None
        if isinstance(seconds, (int,float)) and seconds >= 0:
            st.caption(f'정리 경과 {int(seconds)//60}분 · 10초마다 화면 갱신')
        st.caption('매매 기록과 필요한 운영 자료는 보호합니다. 작업 완료 후 저장 기록 대시보드로 전환합니다.')
    progress()


if __name__ == '__main__':
    if '--serve' in sys.argv:
        import os
        args = [sys.executable, '-m', 'streamlit', 'run', __file__, '--server.address', '127.0.0.1', '--server.port', '8501', '--server.headless', 'true']
        os.execv(sys.executable, args)
    else:
        render()
