"""Launch only the saved-record UI; no engine, broker, news or AI tasks."""
import os
from pathlib import Path
import sys

from .notifications import dashboard_environment
from .session_config import load_session_config


def launch():
    config = load_session_config(os.environ.get('SQ_SESSION_CONFIG') or
                                 'config/sessionquant.realtime-paper.example.yaml')
    if config.environment != 'PAPER_LOCAL' or config.database_backend != 'mysql':
        raise ValueError('점검 화면은 MySQL 모의매매 장부만 지원합니다.')
    os.environ.update(SQ_DB_BACKEND=config.database_backend, SQ_ENVIRONMENT=config.environment,
                      SQ_DASHBOARD_READ_ONLY='1')
    app = Path(__file__).parent / 'dashboard' / 'session_app.py'
    return [sys.executable, '-m', 'streamlit', 'run', str(app), '--server.address', '127.0.0.1',
            '--server.port', '8501', '--', '--db', str(config.db_path)], dashboard_environment(database=True)


if __name__ == '__main__':
    command, environment = launch()
    os.execve(sys.executable, command, environment)
