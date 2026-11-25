"""Progress screen has no DB/API credentials and never starts the engine."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from sessionquant.maintenance_dashboard import load_progress
from test_gcp_deployment import DEPLOY, executable


def test_progress_reader_tolerates_partial_missing_and_oversized_files(tmp_path):
    assert load_progress(tmp_path) == {}
    path = tmp_path/'progress.json'
    for body in ('{', '[]', 'null', 'x'*131073):
        path.write_text(body)
        assert load_progress(tmp_path) == {}
    value = {'state': 'PARTIAL', 'scanned': 200, 'deleted': 100}
    path.write_text(json.dumps(value))
    assert load_progress(tmp_path) == value


def test_file_only_screen_renders_without_a_database(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from sessionquant.database.repository import Repository
    def forbidden(*args, **kwargs):
        pytest.fail('maintenance UI must not open DB')
    monkeypatch.setattr(Repository, '__init__', forbidden)
    path = Path(__file__).parents[1]/'sessionquant/maintenance_dashboard.py'
    app = AppTest.from_file(str(path)).run(timeout=15)
    assert not app.exception
    assert app.title[0].value == 'SESSIONQUANT'
    assert len(app.metric) == 2


@pytest.mark.skipif(os.name == 'nt', reason='Linux lifecycle')
@pytest.mark.parametrize('existing', ['none', 'ours', 'unrelated'])
def test_progress_container_has_only_read_only_files_and_no_credentials(tmp_path, existing):
    data, bindir = tmp_path/'data', tmp_path/'bin'
    data.mkdir(); bindir.mkdir()
    calls = tmp_path/'calls'
    executable(bindir/'docker', '''echo "$*" >> "$CALLS"
if [[ "$*" == 'container inspect '* ]]; then [[ "$EXISTING" != none ]]; exit $?; fi
if [[ "$*" == inspect* ]]; then
    if [[ "$EXISTING" == ours ]]; then echo '["python","-m","sessionquant.maintenance_dashboard","--serve"]'; else echo '["unrelated"]'; fi
fi
''')
    executable(bindir/'install', 'exit 0\n')
    script = tmp_path/'ui.sh'
    script.write_text((DEPLOY/'maintenance_ui.sh').read_text().replace('/var/lib/sessionquant/app/maintenance', str(data)))
    env = {**os.environ, 'PATH': str(bindir)+':'+os.environ['PATH'], 'CALLS': str(calls), 'EXISTING': existing}
    result = subprocess.run(['bash', str(script), 'start'], env=env, capture_output=True, text=True, timeout=10)
    log = calls.read_text()
    if existing == 'unrelated':
        assert result.returncode and 'rm -f' not in log and 'run --rm' not in log
    else:
        assert result.returncode == 0, result.stderr
        assert '--read-only' in log and '--cap-drop ALL' in log
        assert '/app/maintenance:ro' in log and '--env-file' not in log
        assert 'compose' not in log and 'python -m sessionquant.maintenance_dashboard --serve' in log


@pytest.mark.skipif(os.name == 'nt', reason='Linux lifecycle')
@pytest.mark.parametrize('calendar_code', [0, 1])
def test_catchup_uses_detached_bounded_service_and_refuses_open_market(tmp_path, calendar_code):
    bindir = tmp_path/'bin'; bindir.mkdir()
    calls = tmp_path/'calls'
    executable(bindir/'docker', f'echo "docker $*" >> "$CALLS"\nexit {calendar_code}\n')
    executable(bindir/'systemd-run', 'echo "systemd-run $*" >> "$CALLS"\n')
    script = tmp_path/'catchup.sh'
    script.write_text((DEPLOY/'maintenance_now.sh').read_text().replace('/opt/sessionquant', str(tmp_path))
                      .replace('$EUID == 0', '1 == 1'))
    result = subprocess.run(['bash', str(script), '--catch-up'], capture_output=True, text=True, timeout=10,
        env={**os.environ, 'PATH': str(bindir)+':'+os.environ['PATH'], 'CALLS': str(calls)})
    log = calls.read_text()
    assert '--network none' in log
    if calendar_code:
        assert result.returncode and 'systemd-run' not in log
    else:
        assert result.returncode == 0, result.stderr
        assert 'TimeoutStartSec=34200' in log and 'SQ_MAINTENANCE_SECONDS=21600' in log
        assert '--no-block' in log
        assert '/stop.sh' in log and '/start.sh' not in log
        assert 34200 > 21600 + 2*5400 + 180 + 480 + 120
