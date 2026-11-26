from contextlib import closing
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.dashboard import access, shared_read
from sessionquant.dashboard.session_app import DashboardData


@pytest.fixture
def shared(monkeypatch, tmp_path):
    monkeypatch.setenv('SQ_DASHBOARD_ACCESS', 'shared')
    monkeypatch.setenv('SQ_DASHBOARD_ADMIN_HASH', access.password_hash('shared test password 123'))
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    monkeypatch.setenv('SQ_ENVIRONMENT', 'PAPER_LOCAL')
    monkeypatch.delenv('SQ_DASHBOARD_READ_ONLY', raising=False)
    monkeypatch.setenv('SQ_DB', str(tmp_path/'dashboard.db'))
    access._ATTEMPTS.clear()
    shared_read.snapshot.clear()
    from sessionquant.database.repository import Repository
    with closing(Repository(tmp_path/'dashboard.db')) as repo:
        repo.set_control('sessionquant_context', {'environment': 'PAPER_LOCAL'})
        repo.save_state({'environment': 'PAPER_LOCAL', 'broker': {'positions': {}, 'cash': '100'}})
    yield
    shared_read.snapshot.clear()
    access._ATTEMPTS.clear()


def test_anonymous_can_visit_every_page_without_any_write(shared, monkeypatch):
    from sessionquant.database.repository import Repository
    from sessionquant.dashboard.overview import PAGES
    write = Mock(side_effect=AssertionError('anonymous UI must not write'))
    monkeypatch.setattr(Repository, 'set_control', write)
    monkeypatch.setattr(Repository, 'update_control', write)
    monkeypatch.setattr(Repository, 'record', write)
    app = AppTest.from_file(str(Path('sessionquant/dashboard/session_app.py').resolve()), default_timeout=30).run()
    assert not app.exception
    assert app.radio(key='dashboard_page').options == list(PAGES)
    assert app.button(key='public-start').disabled
    assert app.button(key='public-stop').disabled
    for page in PAGES[1:]+PAGES[:1]:
        app.radio(key='dashboard_page').set_value(page).run()
        assert not app.exception, (page, app.exception)
        assert app.radio(key='dashboard_page').value == page
    # News cost reads stay available; the form that saves money notes does not.
    app.radio(key='dashboard_page').set_value('뉴스·LLM').run()
    app.radio(key='news_detail').set_value('LLM 사용량').run()
    assert not app.exception
    assert not any(b.key in {'save-openai-deposit', 'manual-credit-save'} for b in app.button)
    write.assert_not_called()


def test_shared_snapshot_is_read_only_copy_and_admin_bypasses_it(shared, monkeypatch):
    from sessionquant.dashboard import session_app
    source = DashboardData(connected=True)
    source.tables['orders'] = [{'id': 'one'}, {'id': 'two'}]
    load = Mock(return_value=source)
    monkeypatch.setattr(session_app, 'load_database', load)
    first = shared_read.read_page('fixture', page='매매 내역')
    first.tables['orders'].clear()
    second = shared_read.read_page('fixture', page='매매 내역')
    assert len(second.tables['orders']) == 2
    assert load.call_count == 1
    monkeypatch.setattr(access, 'is_admin', lambda: True)
    shared_read.read_page('fixture', page='매매 내역')
    assert load.call_count == 2


def protected_save_fixture():
    import streamlit as st
    from sessionquant.dashboard.access import render_login
    from sessionquant.dashboard_mode import require_writable_dashboard
    render_login()
    if st.button('save test'):
        try:
            require_writable_dashboard()
            st.success('write permitted')
        except (PermissionError, ValueError) as exc:
            st.error(str(exc))


def test_shared_session_expiry_and_inspection_cannot_write(shared, monkeypatch):
    app = AppTest.from_function(protected_save_fixture).run()
    app.button[-1].click().run()
    assert '관리자' in app.error[0].value
    app.text_input(key='sq_admin_password').set_value('shared test password 123')
    app.button[0].click().run()
    app.button[-1].click().run()
    assert app.success[0].value == 'write permitted'
    monkeypatch.setenv('SQ_DASHBOARD_READ_ONLY', '1')
    app.button[-1].click().run()
    assert app.error and not app.success
    monkeypatch.delenv('SQ_DASHBOARD_READ_ONLY')
    # Rotate the server credential: an already-open session cannot save again.
    monkeypatch.setenv('SQ_DASHBOARD_ADMIN_HASH', access.password_hash('new shared password 123'))
    app.button[-1].click().run()
    assert '관리자' in app.error[0].value and not app.success


def command_fixture():
    import os
    import streamlit as st
    from contextlib import closing
    from datetime import datetime, timezone
    from sessionquant.dashboard.access import render_login
    from sessionquant.database.repository import Repository
    from sessionquant.trading_controls import KEY, request_command
    render_login()
    if st.button('send command'):
        try:
            with closing(Repository(os.environ['SQ_DB'], initialize=False)) as repo:
                state = repo.get_control(KEY)
                request_command(repo, 'start', run_id=state['run_id'], environment='PAPER_LOCAL',
                                now=datetime(2026, 11, 25, 16, tzinfo=timezone.utc))
            st.success('queued')
        except (PermissionError, ValueError) as exc:
            st.error(str(exc))


def test_only_authenticated_session_can_enqueue_real_command(shared):
    import os
    from datetime import datetime, timezone
    from sessionquant.database.repository import Repository
    from sessionquant.trading_controls import begin_session
    with closing(Repository(os.environ['SQ_DB'], initialize=False)) as repo:
        begin_session(repo, mode='paper', now=datetime(2026, 11, 25, 16, tzinfo=timezone.utc))
    app = AppTest.from_function(command_fixture).run()
    app.button[-1].click().run()
    assert app.error
    with closing(Repository(os.environ['SQ_DB'], initialize=False)) as repo:
        assert repo.recent('order_intents') == []
    app.text_input(key='sq_admin_password').set_value('shared test password 123')
    app.button[0].click().run()
    app.button[-1].click().run()
    assert not app.exception and app.success[0].value == 'queued'
    app.button(key='sq_logout').click().run()
    app.button[-1].click().run()
    assert app.error
    with closing(Repository(os.environ['SQ_DB'], initialize=False)) as repo:
        rows = repo.recent('order_intents')
        assert len(rows) == 1 and rows[0]['requested_by'] == 'dashboard_admin'


@pytest.mark.parametrize('command,credentials_ok,expected', [
    ('["sh","-c","run engine"]', True, False),
    ('["python","-m","sessionquant.inspection_dashboard"]', False, False),
    ('["python","-m","sessionquant.inspection_dashboard"]', True, True)])
def test_migration_checks_running_image_before_modifying_env(monkeypatch, tmp_path, command, credentials_ok, expected):
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(Path('deploy/gcp').resolve()))
    # Use real env_update against a temporary file; preserve existing hash/API values.
    spec = importlib.util.spec_from_file_location('shared_setup_test', 'deploy/gcp/shared_setup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(module.os, 'uname', lambda: SimpleNamespace(nodename='sessionquant-paper'))
    monkeypatch.setattr(module, 'Path', lambda _: tmp_path)
    (tmp_path/'public-enabled').touch()
    env = tmp_path/'app.env'
    original = "SQ_DASHBOARD_ACCESS='admin'\nSQ_DASHBOARD_ADMIN_HASH='keep-hash'\nAPI_KEY='keep-secret'\n"
    env.write_text(original)
    def verify(**kwargs):
        assert kwargs == {'allow_admin': True}
        if not credentials_ok:
            raise ValueError('credential missing')
    monkeypatch.setattr(module, 'verify_shared_runtime', verify)
    monkeypatch.setattr(module.subprocess, 'check_output', lambda args, **kw:
                        command if args[0] == 'docker' else '{}')
    if expected:
        module.main()
        assert "SQ_DASHBOARD_ACCESS='shared'" in env.read_text()
        assert "SQ_DASHBOARD_ADMIN_HASH='keep-hash'" in env.read_text()
        assert "API_KEY='keep-secret'" in env.read_text()
        assert env.stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises((SystemExit, ValueError)):
            module.main()
        assert env.read_text() == original


def routes_module():
    spec = importlib.util.spec_from_file_location('shared_routes', 'deploy/gcp/check_public_routes.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_shared_profile_permits_8443_to_authenticated_listener():
    routes = routes_module()
    config = {'AllowFunnel': {'node:8443': True}, 'Web': {
        'node:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8501'}}}}}
    with pytest.raises(ValueError):
        routes.validate(config, ready=True)
    routes.validate(config, ready=True, shared=True)
    config['AllowFunnel']['node:443'] = True
    with pytest.raises(ValueError):
        routes.validate(config, ready=True, shared=True)
    del config['AllowFunnel']['node:443']
    config['Web']['node:8443']['Handlers']['/extra'] = {'Proxy': 'http://127.0.0.1:8501'}
    with pytest.raises(ValueError):
        routes.validate(config, ready=True, shared=True)


@pytest.mark.parametrize('role,hashed,xsrf,allowed', [
    ('shared', True, 'true', True), ('local', True, 'true', False),
    ('admin', True, 'true', False), ('shared', False, 'true', False),
    ('shared', True, 'false', False), ('unknown', True, 'true', False)])
def test_live_image_verification_fails_closed(monkeypatch, role, hashed, xsrf, allowed):
    routes = routes_module()
    monkeypatch.setenv('SQ_DASHBOARD_ACCESS', role)
    monkeypatch.setenv('SQ_DASHBOARD_ADMIN_HASH', access.password_hash('shared test password 123') if hashed else '')
    monkeypatch.setenv('STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION', xsrf)
    monkeypatch.setenv('STREAMLIT_SERVER_ENABLE_CORS', 'true')
    def run(args, **kwargs):
        assert args[:2] == ['docker', 'compose']
        exec(args[-1], {})
    monkeypatch.setattr(routes.subprocess, 'run', run)
    if allowed:
        routes.verify_shared_runtime()
    else:
        with pytest.raises(AssertionError):
            routes.verify_shared_runtime()
