"""Isolated, immutable viewer process. It has no admin identity or trading API credentials."""
import os
from pathlib import Path
import sys


def launch():
    # Do not allow a forwarded header, query parameter, password, or env overlay to elevate this process.
    os.environ.update(SQ_DASHBOARD_ACCESS='viewer', SQ_DASHBOARD_READ_ONLY='1',
                      SQ_DB_BACKEND='mysql', SQ_ENVIRONMENT='PAPER_LOCAL')
    os.environ.pop('SQ_DASHBOARD_ADMIN_HASH', None)
    from .notifications import dashboard_environment
    app = Path(__file__).parent / 'dashboard' / 'session_app.py'
    return [sys.executable, '-m', 'streamlit', 'run', str(app), '--server.address', '127.0.0.1',
            '--server.port', '8502'], dashboard_environment(database=True)


if __name__ == '__main__':
    command, environment = launch()
    os.execve(sys.executable, command, environment)
