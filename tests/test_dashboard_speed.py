"""Exercise the deployed file entry point, rather than importing its renderer."""
from contextlib import closing
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.database.repository import Repository


@pytest.mark.parametrize('events', [[], [{
    'at': '2026-11-23T14:40:00+00:00', 'reasons': ['CANDLE_LATE'],
    'symbols': [{'symbol': 'AAPL'}], 'affected_count': 1,
    'cycle_seconds': 2.5, 'phases': {'api_http': 1.2, 'db_query': 0.1},
}]])
def test_speed_tab_from_file_and_restored_url(tmp_path, monkeypatch, events):
    path = tmp_path/'speed.db'
    archive = {'events': events}
    with closing(Repository(path, backend='sqlite')) as repo:
        repo.set_control('sessionquant_context', {'environment': 'PAPER_LOCAL'})
        repo.set_control('candle_delay_incidents_v1', archive)
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    monkeypatch.setattr('sys.argv', ['session_app.py', '--db', str(path)])
    entry = str(Path('sessionquant/dashboard/session_app.py').resolve())
    app = AppTest.from_file(entry, default_timeout=30).run()
    app.radio(key='dashboard_page').set_value('시장·종목').run()
    app.radio(key='market_detail').set_value('처리 속도').run()
    assert not app.exception
    assert any(x.value == '자동 보관한 분봉 지연 기록' for x in app.subheader)
    if events:
        assert any('AAPL' in frame.value.to_string() for frame in app.dataframe)
    else:
        assert any(x.value == '아직 자동 보관한 지연 기록이 없습니다.' for x in app.info)
    restored = AppTest.from_file(entry, default_timeout=30)
    restored.query_params.update(app.query_params)
    restored.run()
    assert not restored.exception
    assert restored.radio(key='market_detail').value == '처리 속도'
    with closing(Repository(path, backend='sqlite')) as repo:
        assert repo.get_control('candle_delay_incidents_v1') == archive
        assert not repo.all('orders') and not repo.all('order_intents')
