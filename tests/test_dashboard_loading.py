"""상세 화면의 불필요한 대량 조회와 부분 실패를 검증한다."""
from datetime import datetime, timezone

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.session_app import load_database
from sessionquant.database.mysql_repository import MySQLRepositoryError


@pytest.fixture
def database(monkeypatch):
    import sessionquant.database.config as config
    import sessionquant.database.repository as module

    class FakeRepository:
        calls = []
        failure = None
        controls = {
            'sessionquant_context': {'environment': 'PAPER_LOCAL', 'updated_at': '2026-11-18T05:40:00Z'},
            'watchlist_dashboard_v1': {'status': 'VALID', 'operating_count': 25},
            'market_data_quality_v1': {'updated_at': '2026-11-18T05:40:00Z'},
        }
        def __init__(self, path, *, initialize=True):
            assert initialize is False
            self.closed = False
            self.__class__.instances.append(self)
        def load_state(self):
            return {'broker': {'cash': '100', 'positions': {}}}
        def get_control(self, name, default=None):
            self.calls.append(name)
            if name == self.failure:
                raise MySQLRepositoryError(Exception(2013, 'PRIVATE password=secret'), 'SELECT')
            return self.controls.get(name, default)
        def recent(self, kind, limit):
            self.calls.append((kind, limit))
            if kind == self.failure:
                raise MySQLRepositoryError(Exception(2013, 'PRIVATE password=secret'), 'SELECT')
            return [{'symbol': 'SPY', 'bid': '500', 'ask': '501'}] if kind == 'quote_snapshots' else []
        def recent_llm_attempts(self, limit):
            self.calls.append(('llm_attempts', limit))
            return []
        def close(self):
            self.closed = True

    FakeRepository.instances = []
    monkeypatch.setattr(config, 'database_backend', lambda: 'mysql')
    monkeypatch.setattr(module, 'Repository', FakeRepository)
    return FakeRepository


def test_market_view_does_not_fetch_unused_history_or_news(database):
    database.failure = 'news_events'
    data = load_database('unused.db', page='시장·종목')
    assert data.connected and data.error is None
    assert data.tables['quote_snapshots'][0]['symbol'] == 'SPY'
    assert data.watchlist_state['operating_count'] == 25
    journals = [call for call in database.calls if isinstance(call, tuple)]
    assert {kind for kind, _ in journals} == {
        'market_regimes', 'quote_snapshots', 'candidates', 'economic_events', 'fred_reference_observations'}
    assert sum(limit for _, limit in journals) <= 850
    assert all(repo.closed for repo in database.instances)


@pytest.mark.parametrize('page,excluded', [
    ('대시보드', 'market_bars'), ('전략·백테스트', 'news_events'),
    ('매매 내역', 'pipeline_status'), ('뉴스·LLM', 'market_bars'),
    ('시스템·설정', 'news_events'),
])
def test_each_page_skips_unrelated_records(database, page, excluded):
    database.failure = excluded
    data = load_database('unused.db', page=page)
    assert data.connected and data.error is None
    assert not any(isinstance(call, tuple) and call[0] == excluded for call in database.calls)
    assert all(repo.closed for repo in database.instances)


def test_disconnect_retains_completed_reads_and_recovers_next_refresh(database, caplog):
    database.failure = 'candidates'
    data = load_database('unused.db', page='시장·종목')
    assert not data.connected
    assert data.state['broker']['cash'] == '100'
    assert data.context['updated_at'] == '2026-11-18T05:40:00Z'
    assert data.watchlist_state['operating_count'] == 25
    assert data.tables['quote_snapshots'][0]['bid'] == '500'
    assert {'candidates', 'economic_events', 'fred_references'} <= data.unavailable
    assert 'quote_snapshots' not in data.unavailable
    assert database.calls[-1] == ('candidates', 300)  # 끊긴 연결로 후속 조회하지 않는다.
    assert '기술 분석 이력' in data.error and '2013' in data.error
    assert 'source=candidates' in caplog.text and 'mysql_code=2013' in caplog.text
    assert 'PRIVATE' not in data.error + caplog.text
    assert 'secret' not in data.error + caplog.text
    assert database.instances[-1].closed
    database.failure = None
    database.controls['watchlist_dashboard_v1'] = {'operating_count': 24}
    refreshed = load_database('unused.db', page='시장·종목')
    assert refreshed.connected and refreshed.error is None and not refreshed.unavailable
    assert refreshed.watchlist_state['operating_count'] == 24
    assert len(database.instances) == 2 and all(repo.closed for repo in database.instances)


def render_market_fixture():
    from sessionquant.dashboard.session_app import render_dashboard
    render_dashboard('unused.db', selected_page='시장·종목')


def test_partial_market_screen_preserves_watchlist_and_does_not_invent_zero_signals(database):
    now = datetime.now(timezone.utc).isoformat()
    database.controls['watchlist_dashboard_v1']['updated_at'] = now
    database.failure = 'candidates'
    app = AppTest.from_function(render_market_fixture, default_timeout=30).run()
    assert not app.exception
    assert any('기술 분석 이력' in row.value for row in app.error)
    assert any(metric.label == '유효 운영 종목' and metric.value == '25 / 50' for metric in app.metric)
    assert any(metric.label == '현재 진입 신호' and metric.value == '—' for metric in app.metric)
    assert any('조회 미완료' in row.value for row in app.info)
    assert not any('저장된 실행 기록이 없습니다' in row.value for row in app.info)
    assert any(row.value == '시장 참고 ETF · 거래 후보 제외' for row in app.subheader)
    assert not any('저장된 FRED 일별 참고 자료가 없습니다' in row.value for row in app.caption)


def render_trade_fixture():
    from sessionquant.dashboard.session_app import render_dashboard
    render_dashboard('unused.db', selected_page='매매 내역')


def test_incomplete_trade_snapshot_does_not_enable_controls_or_show_empty_positions(database):
    database.failure = 'orders'
    app = AppTest.from_function(render_trade_fixture, default_timeout=30).run()
    assert not app.exception and app.error
    assert not app.button
    assert not any('포지션이 없습니다' in row.value for row in app.info)


def test_unrecognized_page_does_not_open_database(database):
    with pytest.raises(ValueError):
        load_database('unused.db', page='invalid')
    assert not database.instances


def render_news_fixture():
    from sessionquant.dashboard.session_app import render_dashboard
    render_dashboard('unused.db', selected_page='뉴스·LLM')


def test_news_navigation_only_reads_selected_details(database, monkeypatch):
    import sessionquant.dashboard.llm_panel as llm
    import sessionquant.dashboard.summary_panels as summary
    calls = []
    monkeypatch.setattr(summary, 'open_cost_repository', lambda *a, **kw: None)
    monkeypatch.setattr(llm, 'render_llm_details', lambda *a, **kw: calls.append('usage'))
    app = AppTest.from_function(render_news_fixture, default_timeout=30).run()
    assert not app.exception and not calls
    assert ('news_events', 2000) in database.calls
    assert ('macro_news', 2000) not in database.calls
    database.calls.clear()
    app.radio(key='news_detail').set_value('경제·정치·국제정세').run()
    assert not app.exception and not calls
    assert ('macro_news', 2000) in database.calls
    assert ('news_events', 2000) not in database.calls
    database.calls.clear()
    app.radio(key='news_detail').set_value('LLM 사용량').run()
    assert not app.exception and calls == ['usage']
    assert ('news_events', 2000) not in database.calls and ('macro_news', 2000) not in database.calls
    # 모든 하위 화면에서 진단 집계 범위는 동일하게 유지한다.
    assert ('news_ingestions', 2000) in database.calls and ('llm_attempts', 500) in database.calls


def test_existing_sqlite_dashboard_connection_never_creates_empty_database(tmp_path):
    import sqlite3
    from sessionquant.database.repository import Repository
    path = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        Repository(path, backend='sqlite', initialize=False)
    assert not path.exists()
    writer = Repository(path, backend='sqlite')
    writer.set_control('trade_control_state', {'status': 'WAITING'})
    reader = Repository(path, backend='sqlite', initialize=False)
    try:
        assert reader.get_control('trade_control_state')['status'] == 'WAITING'
        writer.set_control('trade_control_state', {'status': 'STOPPED'})
        assert reader.get_control('trade_control_state')['status'] == 'STOPPED'
        with reader.transaction():
            reader.set_control('dashboard_request', {'action': 'start'})
        assert writer.get_control('dashboard_request') == {'action': 'start'}
    finally:
        reader.close()
        writer.close()
