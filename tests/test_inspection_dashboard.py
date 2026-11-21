"""Inspection has the ordinary UI but cannot queue trading or call providers."""
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.dashboard_mode import inspection_start_message
from sessionquant.database.repository import Repository
from sessionquant.trading_controls import begin_session, enqueue, request_command


@pytest.mark.parametrize('stamp,message', [
    ('2026-11-21T16:00:00+00:00', '휴장일'),
    ('2026-11-20T21:00:00+00:00', '정규장이 종료'),
    ('2026-11-27T18:00:00+00:00', '정규장이 종료'),
    ('2026-11-23T13:00:00+00:00', '점검 화면'),
])
def test_inspection_start_button_explains_calendar_without_repository_access(monkeypatch, stamp, message):
    monkeypatch.setenv('SQ_DASHBOARD_READ_ONLY', '1')
    now = datetime.fromisoformat(stamp)
    assert message in inspection_start_message(now)
    app = AppTest.from_string(f'''
from datetime import datetime
from sessionquant.trading_controls import render_controls
render_controls(None, positions=[], now=datetime.fromisoformat({stamp!r}), compact=True)
''').run()
    app.button(key='trading-start').click().run()
    assert not app.exception
    assert message in app.info[0].value
    assert app.button(key='trading-stop').disabled


@pytest.mark.parametrize('action', ['start', 'stop_all', 'sell_symbol'])
def test_inspection_rejects_direct_commands_even_with_fresh_armed_ledger(tmp_path, monkeypatch, action):
    now = datetime.now(timezone.utc)
    with closing(Repository(tmp_path/'ledger.db', backend='sqlite')) as repo:
        control = begin_session(repo, mode='paper', now=now)
        monkeypatch.setenv('SQ_DASHBOARD_READ_ONLY', '1')
        with pytest.raises(ValueError):
            enqueue(repo, action, run_id=control['run_id'], symbol='AAPL', now=now)
        with pytest.raises(ValueError):
            request_command(repo, action, run_id=control['run_id'], environment='PAPER_LOCAL',
                            symbol='AAPL', now=now)
        assert repo.all('order_intents') == []
        assert repo.get_control('trade_control_state') == control


def test_launcher_preserves_namespace_without_broker_credentials(monkeypatch):
    import sessionquant.inspection_dashboard as launcher
    config = SimpleNamespace(environment='PAPER_LOCAL', database_backend='mysql', db_path=Path('data/test.db'))
    monkeypatch.setattr(launcher, 'load_session_config', lambda path: config)
    for key, value in {'SQ_MYSQL_PASSWORD': 'db-secret', 'TOSS_CLIENT_SECRET': 'broker-secret',
                       'OPENAI_API_KEY': 'ai-secret', 'SQ_DB_NAMESPACE': 'selected-ledger',
                       'SQ_DASHBOARD_READ_ONLY': '0', 'SQ_DB_BACKEND': 'mysql',
                       'SQ_ENVIRONMENT': 'PAPER_LOCAL'}.items():
        monkeypatch.setenv(key, value)
    args, env = launcher.launch()
    assert args[1:4] == ['-m', 'streamlit', 'run']
    assert args[-2:] == ['--db', 'data/test.db']
    assert env['SQ_DASHBOARD_READ_ONLY'] == '1'
    assert env['SQ_DB_NAMESPACE'] == 'selected-ledger'
    assert env['SQ_MYSQL_PASSWORD'] == 'db-secret'
    assert 'TOSS_CLIENT_SECRET' not in env and 'OPENAI_API_KEY' not in env
    config.environment = 'LIVE_LOCAL'
    with pytest.raises(ValueError): launcher.launch()


def test_calendar_error_explains_why_start_is_blocked(monkeypatch):
    import sessionquant.market.bars as bars
    def broken(now): raise ValueError('calendar unavailable')
    monkeypatch.setattr(bars, 'session_bounds', broken)
    assert '일정을 확인하지 못했습니다' in inspection_start_message()
