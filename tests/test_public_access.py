from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.dashboard import access
from sessionquant.dashboard.session_app import DashboardData


@pytest.fixture(autouse=True)
def clean_auth():
    access._ATTEMPTS.clear()
    yield
    access._ATTEMPTS.clear()


def test_credentials_expiry_rotation_and_forgery(monkeypatch):
    monkeypatch.setenv('SQ_DASHBOARD_ADMIN_HASH', access.password_hash('a long test password 123'))
    state = {}
    assert not access.login(state, 'wrong password 123', now=10)
    assert access.login(state, 'a long test password 123', now=10)
    assert access.authenticated(state, now=11)
    assert not access.authenticated(state, now=3610)
    assert not access.authenticated({access._GRANT: (3610.0, 'forged')}, now=11)
    monkeypatch.setenv('SQ_DASHBOARD_ADMIN_HASH', access.password_hash('rotated password 123'))
    assert not access.authenticated(state, now=11)


def test_login_limit_is_shared_between_sessions(monkeypatch):
    monkeypatch.setenv('SQ_DASHBOARD_ADMIN_HASH', access.password_hash('a long test password 123'))
    for _ in range(5):
        assert not access.login({}, 'wrong password 123', now=10)
    assert not access.login({}, 'a long test password 123', now=11)
    assert access.login({}, 'a long test password 123', now=70)


@pytest.mark.parametrize('role', ['viewer', 'admin', 'shared', 'typo'])
def test_direct_command_rejected_before_database(monkeypatch, role):
    from sessionquant.trading_controls import request_command, enqueue
    monkeypatch.setenv('SQ_DASHBOARD_ACCESS', role)
    repo = Mock()
    for action in ('start', 'sell_symbol', 'stop_all'):
        with pytest.raises(PermissionError):
            request_command(repo, action, run_id='test', environment='PAPER_LOCAL')
        with pytest.raises(PermissionError):
            enqueue(repo, action, run_id='test')
    assert not repo.mock_calls


def login_fixture():
    import streamlit as st
    from sessionquant.dashboard.access import render_login, require_admin, is_admin
    render_login()
    st.write('role='+str(is_admin()))
    if st.button('protected action'):
        try:
            require_admin()
            st.success('authorized')
        except PermissionError:
            st.error('denied')


@pytest.mark.parametrize('role', ['admin', 'shared'])
def test_admin_login_logout_and_viewer_cannot_login(monkeypatch, role):
    monkeypatch.setenv('SQ_DASHBOARD_ACCESS', role)
    monkeypatch.setenv('SQ_DASHBOARD_ADMIN_HASH', access.password_hash('a long test password 123'))
    app = AppTest.from_function(login_fixture).run()
    app.button[-1].click().run()
    assert app.error[0].value == 'denied'
    app.text_input(key='sq_admin_password').set_value('a long test password 123')
    app.button[0].click().run()
    assert not app.exception
    assert any('role=True' in r.value for r in app.markdown)
    app.button[-1].click().run()
    assert app.success[0].value == 'authorized'
    app.button(key='sq_logout').click().run()
    assert any('role=False' in r.value for r in app.markdown)
    monkeypatch.setenv('SQ_DASHBOARD_ACCESS', 'viewer')
    app = AppTest.from_function(login_fixture).run()
    assert not app.text_input
    assert any('role=False' in r.value for r in app.markdown)


def test_public_page_cannot_navigate_or_show_raw_errors(monkeypatch):
    from sessionquant.dashboard import public_view
    monkeypatch.setenv('SQ_DASHBOARD_ACCESS', 'viewer')
    data = DashboardData(connected=True, context={'environment': 'PAPER_LOCAL'},
                         state={'environment': 'PAPER_LOCAL', 'broker': {'positions': {}, 'cash': '100'}})
    monkeypatch.setattr(public_view, 'snapshot', lambda _: data)
    app = AppTest.from_file(str(Path('sessionquant/dashboard/session_app.py').resolve()), default_timeout=30)
    app.query_params['page'] = 'system'
    app.session_state['dashboard_raw_data'] = True
    app.run()
    assert not app.exception
    assert not app.radio
    assert not app.text_input
    assert not any(b.label in {'매매 시작', '전체 매도', '일괄 매매 종료·청산'} for b in app.button)
    assert any(m.label == '모의 총자산' for m in app.metric)
    data.error = 'mysql SECRET_INTERNAL_ERROR'
    app.run()
    assert not any('SECRET_INTERNAL_ERROR' in str(e.value) for e in app.get('markdown')+app.get('info'))


def test_admin_start_rechecks_market_calendar(monkeypatch):
    from sessionquant.trading_controls import enqueue
    monkeypatch.setenv('SQ_DASHBOARD_ACCESS', 'admin')
    monkeypatch.setattr(access, 'is_admin', lambda: True)
    repo = Mock()
    with pytest.raises(ValueError, match='정규장'):
        enqueue(repo, 'start', run_id='test', now=datetime(2026, 11, 26, 15, tzinfo=timezone.utc))
    assert not repo.mock_calls


def test_public_launcher_discards_admin_identity(monkeypatch):
    from sessionquant.public_dashboard import launch
    for key in ('SQ_DASHBOARD_READ_ONLY', 'SQ_DB_BACKEND', 'SQ_ENVIRONMENT'):
        monkeypatch.setenv(key, 'test-original')
    monkeypatch.setenv('SQ_DASHBOARD_ACCESS', 'admin')
    monkeypatch.setenv('SQ_DASHBOARD_ADMIN_HASH', 'private')
    monkeypatch.setenv('TOSS_SECRET_KEY', 'private')
    command, env = launch()
    assert '8502' in command
    assert env['SQ_DASHBOARD_ACCESS'] == 'viewer'
    assert 'SQ_DASHBOARD_ADMIN_HASH' not in env
    assert 'TOSS_SECRET_KEY' not in env


def test_admin_hash_only_passes_to_dashboard_database_process(monkeypatch):
    from sessionquant.notifications import dashboard_environment
    monkeypatch.setenv('SQ_DASHBOARD_ADMIN_HASH', 'hash-only')
    monkeypatch.setenv('OPENAI_API_KEY', 'never-forward')
    assert 'SQ_DASHBOARD_ADMIN_HASH' not in dashboard_environment()
    assert dashboard_environment(database=True)['SQ_DASHBOARD_ADMIN_HASH'] == 'hash-only'
    assert 'OPENAI_API_KEY' not in dashboard_environment(database=True)


def test_public_read_plan_excludes_audit_logs_and_raw_news(tmp_path, monkeypatch):
    from sessionquant.database.repository import Repository
    from sessionquant.dashboard.session_app import load_database
    path = tmp_path/'public-read.db'
    from contextlib import closing
    with closing(Repository(path)) as repo:
        repo.set_control('sessionquant_context', {'environment': 'PAPER_LOCAL'})
        repo.set_control('strategy_explanation_v1', {'status': 'READY'})
        repo.save_state({'environment': 'PAPER_LOCAL', 'broker': {'positions': {}, 'cash': '100'}})
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    original = Repository.recent
    calls = []
    def recent(self, kind, limit=100):
        assert kind in {'executions', 'quote_snapshots'}
        calls.append(kind)
        return original(self, kind, limit)
    monkeypatch.setattr(Repository, 'recent', recent)
    data = load_database(path, page='대시보드', panel='public')
    assert data.connected and not data.error
    assert data.strategy_explanation['status'] == 'READY'
    assert set(calls) == {'executions', 'quote_snapshots'}
