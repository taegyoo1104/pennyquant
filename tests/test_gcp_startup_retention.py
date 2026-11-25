"""Host startup/update ordering and real handoff/lock boundaries without Docker."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

from test_gcp_deployment import DEPLOY, executable, install_lifecycle_lock


@pytest.fixture
def lifecycle(tmp_path):
    root = tmp_path/'repo'
    deploy = root/'deploy/gcp'
    deploy.mkdir(parents=True)
    etc = tmp_path/'etc'
    (etc/'systemd/system').mkdir(parents=True)
    data = tmp_path/'data'
    (data/'health').mkdir(parents=True)
    (data/'app').mkdir()
    for name in ('app.env', 'db.env'):
        (etc/name).write_text('fixture=private\n')
    (etc/'backup-status').write_text('UPLOADED previous\n')
    bindir = tmp_path/'bin'
    bindir.mkdir()
    calls = tmp_path/'calls'
    calls.touch()
    env = {**os.environ, 'PATH': str(bindir)+os.pathsep+os.environ['PATH'],
           'CALLS': str(calls), 'ETC': str(etc), 'DATA': str(data),
           'ROOT': str(root), 'ARCHIVE': str(tmp_path/'archive'),
           'START': str(deploy/'start.sh'), 'READY': str(tmp_path/'ready'),
           'RELEASE': str(tmp_path/'release'), 'MAINTAINED': str(tmp_path/'maintained'),
           'HANDOFF': str(tmp_path/'startup-prepared.json'),
           'STATUS': 'CLEAR', 'RUNTIME': 'none', 'AFTER_DELAY': '3600',
           'BLOCK': '', 'FAILURE': '', 'HANDOFF_CHANGE': '', 'CHECKPOINT': 'absent'}

    def confined(body):
        return (body.replace('/opt/sessionquant', str(root))
                .replace('/etc/sessionquant', str(etc))
                .replace('/etc/systemd/system', str(etc/'systemd/system'))
                .replace('/var/lib/sessionquant', str(data))
                .replace('/run/sessionquant-lifecycle.lock', str(tmp_path/'lifecycle.lock'))
                .replace('/run/sessionquant-update.lock', str(tmp_path/'update.lock'))
                .replace('/run/sessionquant-stop.lock', str(tmp_path/'stop.lock'))
                .replace('/run/sessionquant-startup-prepared.json', str(tmp_path/'startup-prepared.json'))
                .replace('/var/tmp/sessionquant-update.', str(tmp_path/'stage.')))

    for name in ('start.sh', 'stop.sh', 'inspect.sh', 'update_preopen.sh', 'startup_handoff.py'):
        body = confined((DEPLOY/name).read_text())
        if name == 'update_preopen.sh':
            body = body.replace('$EUID == 0', '1 == 1')
            host_check = next(line for line in body.splitlines() if '/sys/class/dmi' in line)
            body = body.replace(host_check, ': # disposable host fixture')
        if name == 'startup_handoff.py':
            # Preserve all identity/lock checks while allowing non-root test users.
            body = body.replace('os.geteuid() != 0', 'False')
            body = body.replace('info.st_uid != 0', 'info.st_uid != os.geteuid()')
        (deploy/name).write_text(body)
    install_lifecycle_lock(root, tmp_path/'lifecycle.lock')
    (deploy/'sessionquant.service').write_text('[Service]\nType=oneshot\n')
    (deploy/'sessionquant-storage-guard.timer').write_text('[Timer]\nOnBootSec=1h\n')
    (deploy/'update_guard.py').write_text('# guard has separate database tests\n')
    (deploy/'prepare_update.py').write_text('''import os,shutil,sys
from pathlib import Path
with open(os.environ['CALLS'],'a') as stream:
    stream.write('install-code\\n' if '--backup' in sys.argv else 'check-code\\n')
if '--backup' in sys.argv:
    shutil.copytree(Path(__file__).parents[2], os.environ['ROOT'], dirs_exist_ok=True)
''')
    (deploy/'startup_retention.py').write_text('''import json,os,sys,time
from pathlib import Path
with open(os.environ['CALLS'],'a') as stream:
    stream.write('startup-retention '+json.dumps(sys.argv[1:])+'\\n')
if os.environ['BLOCK']:
    Path(os.environ['READY']).touch()
    deadline=time.monotonic()+10
    while not Path(os.environ['RELEASE']).exists():
        if time.monotonic()>deadline:raise SystemExit('fixture release timed out')
        time.sleep(.01)
if os.environ['FAILURE']=='retention':raise SystemExit(7)
Path(os.environ['DATA'],'health/startup-retention-last.json').write_text(
    json.dumps({'status':os.environ['STATUS']}))
Path(os.environ['MAINTAINED']).touch()
''')
    for name, event in (('storage_check.py', 'storage'), ('backup_capacity.py', 'capacity')):
        (deploy/name).write_text(f'''import os
with open(os.environ['CALLS'],'a') as stream:stream.write('{event}\\n')
''')
    (deploy/'nightly_checkpoint.py').write_text('''import os,sys
from pathlib import Path
with open(os.environ['CALLS'],'a') as stream:stream.write('checkpoint-'+sys.argv[1]+'\\n')
if sys.argv[1]=='verify' and os.environ['CHECKPOINT']!='valid':raise SystemExit(1)
if sys.argv[1]=='writable':
    Path(os.environ['ETC'],'nightly-seal.json').unlink(missing_ok=True)
    Path(os.environ['ETC'],'nightly-checkpoint.json').unlink(missing_ok=True)
''')
    executable(deploy/'backup.sh', 'echo backup >> "$CALLS"\nprintf "UPLOADED fresh\\n" > "$ETC/backup-status"\n')
    executable(deploy/'preopen_check.sh', 'echo preopen >> "$CALLS"\n')
    executable(bindir/'python3', f'exec {shlex.quote(sys.executable)} "$@"\n')
    executable(bindir/'git', '''echo "git $*" >> "$CALLS"
if [[ "$*" == *rev-parse* ]]; then printf '%040d\\n' 1
elif [[ "$1" == archive ]]; then tar -cf - -C "$ARCHIVE" .; fi
''')
    executable(bindir/'docker', '''echo "docker $*" >> "$CALLS"
if [[ "$*" == *"--services app"* ]]; then
    [[ "$RUNTIME" != query-failed ]] || exit 42
    if [[ "$RUNTIME" != none ]]; then echo app; fi
elif [[ "$1" == inspect ]]; then
    if [[ "$RUNTIME" == inspection ]]; then echo '["python","-m","sessionquant.inspection_dashboard"]'
    else echo '["sh","-c","exec python -m sessionquant run"]'; fi
elif [[ "$*" == *"market_shutdown --startup"* ]]; then
    echo calendar >> "$CALLS"
    if [[ -e "$MAINTAINED" ]]; then echo "$AFTER_DELAY"; else echo 3600; fi
    [[ "$FAILURE" != calendar ]] || exit 7
elif [[ "$*" == *"SELECT @@GLOBAL.log_bin"* ]]; then echo 0
elif [[ "$*" == *"image inspect sessionquant:local"* ]]; then echo sha256:fixture
fi
''')
    executable(bindir/'systemctl', '''echo "systemctl $*" >> "$CALLS"
if [[ "$1" == is-active && "$*" == *.service ]]; then exit 3; fi
if [[ "$1" == is-enabled ]]; then echo disabled; fi
if [[ "$*" == "start sessionquant.service" ]]; then
    if [[ "$HANDOFF_CHANGE" == stale ]]; then
        python3 - "$HANDOFF" <<'PY'
import json,sys,time
from pathlib import Path
path=Path(sys.argv[1]); value=json.loads(path.read_text())
# Monotonic time has no guaranteed epoch. -1 can be within the 120-second
# window on a freshly started runner; expire relative to this clock instead.
value['at']=time.monotonic()-121
path.write_text(json.dumps(value))
PY
    fi
    # A real systemd service does not inherit the updater's descriptor 9.
    bash "$START" 9>&-
fi
''')
    executable(bindir/'systemd-run', 'echo "timer $*" >> "$CALLS"\n')
    executable(bindir/'sync', 'echo sync >> "$CALLS"\n')
    import shutil
    shutil.copytree(root, tmp_path/'archive')
    return root, etc, data, calls, env


def run_entry(lifecycle, name, *args, **overrides):
    root, _, _, _, env = lifecycle
    return subprocess.run(['bash', str(root/'deploy/gcp'/name), *args],
                          env={**env, **overrides}, capture_output=True, text=True, timeout=15)


def retention_calls(lines):
    return [line for line in lines if line.startswith('startup-retention ')]


def runtime_calls(lines):
    return [line for line in lines if line.endswith(' up -d --wait --wait-timeout 480')
            and 'compose.inspection.yaml' not in line]


@pytest.mark.parametrize('status', ['CLEAR', 'PENDING', 'UNKNOWN', 'DISABLED'])
def test_start_prepares_database_and_retention_before_calendar_timers_and_runtime(lifecycle, status):
    _, etc, _, calls, _ = lifecycle
    (etc/'nightly-seal.json').write_text('{}')
    result = run_entry(lifecycle, 'start.sh', STATUS=status)
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    db = next(i for i, line in enumerate(lines) if line.endswith('180 db'))
    prepare = lines.index('startup-retention []')
    timers = [i for i, line in enumerate(lines) if line.startswith('timer ')]
    app = runtime_calls(lines)
    assert db < prepare < lines.index('calendar') < lines.index('checkpoint-writable') < lines.index(app[0])
    assert len(timers) == 2 and len(app) == 1
    assert prepare < lines.index('capacity') < lines.index('calendar') < timers[0] < timers[1] < lines.index(app[0])
    assert lines.count('storage') == 2 and len(retention_calls(lines)) == 1
    assert 'backup' not in lines


@pytest.mark.parametrize('failure,delay,expected', [('', '0', 0), ('calendar', '3600', 1)])
def test_calendar_is_recomputed_after_maintenance_and_close_opens_only_inspection(lifecycle, failure, delay, expected):
    seal = lifecycle[1]/'nightly-seal.json'
    seal.write_text('{}')
    result = run_entry(lifecycle, 'start.sh', AFTER_DELAY=delay, FAILURE=failure)
    assert result.returncode == expected, result.stderr
    lines = lifecycle[3].read_text().splitlines()
    assert lines.index('startup-retention []') < lines.index('calendar')
    assert not runtime_calls(lines) and not any(line.startswith('timer ') for line in lines)
    assert sum('compose.inspection.yaml up' in line for line in lines) == 1
    assert seal.exists() and 'checkpoint-writable' not in lines


@pytest.mark.parametrize('running', ['engine', 'query-failed'])
def test_start_refuses_running_or_unknown_runtime_before_any_maintenance(lifecycle, running):
    result = run_entry(lifecycle, 'start.sh', RUNTIME=running)
    assert result.returncode != 0
    lines = lifecycle[3].read_text().splitlines()
    assert not retention_calls(lines) and not runtime_calls(lines)
    assert not any('stop -t' in line or ' up ' in line or line.startswith('timer ') for line in lines)
    assert 'storage' not in lines and 'calendar' not in lines


def test_start_stops_inspection_before_retention(lifecycle):
    result = run_entry(lifecycle, 'start.sh', RUNTIME='inspection')
    assert result.returncode == 0, result.stderr
    lines = lifecycle[3].read_text().splitlines()
    stopped = next(i for i, line in enumerate(lines) if 'stop -t 180 app' in line)
    assert stopped < lines.index('startup-retention []') < lines.index(runtime_calls(lines)[0])


def test_failed_startup_maintenance_never_arms_or_starts_runtime(lifecycle):
    result = run_entry(lifecycle, 'start.sh', FAILURE='retention')
    assert result.returncode == 7
    lines = lifecycle[3].read_text().splitlines()
    assert retention_calls(lines) == ['startup-retention []']
    assert not runtime_calls(lines) and 'calendar' not in lines
    assert not any(line.startswith('timer ') for line in lines)


def test_stale_handoff_does_not_skip_standalone_startup_maintenance(lifecycle):
    marker = Path(lifecycle[4]['HANDOFF'])
    marker.write_text(json.dumps({'at': -1}))
    marker.chmod(0o600)
    result = run_entry(lifecycle, 'start.sh')
    assert result.returncode == 0, result.stderr
    assert retention_calls(lifecycle[3].read_text().splitlines()) == ['startup-retention []']
    assert not marker.exists()


@pytest.mark.parametrize('status', ['CLEAR', 'PENDING', 'UNKNOWN', 'DISABLED'])
@pytest.mark.parametrize('checkpoint', ['absent', 'valid'])
def test_update_hands_off_current_operation_once_even_with_pending_cleanup(lifecycle, status, checkpoint):
    _, etc, _, calls, env = lifecycle
    if checkpoint == 'valid':
        (etc/'nightly-checkpoint.json').write_text('{}')
        (etc/'nightly-seal.json').write_text('{}')
    result = run_entry(lifecycle, 'update_preopen.sh', 'a'*40, STATUS=status, CHECKPOINT=checkpoint)
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    expected = [] if checkpoint == 'valid' else ['--backup-before', 'UPLOADED previous']
    maintenance = 'startup-retention '+json.dumps(expected)
    assert retention_calls(lines) == [maintenance]
    assert ('backup' in lines) is (checkpoint == 'absent')
    if checkpoint == 'absent':
        assert lines.index('backup') < lines.index('install-code')
    else:
        assert lines.index('checkpoint-writable') < lines.index(maintenance)
    assert lines.index('install-code') < lines.index(maintenance) < lines.index('preopen')
    assert lines.index('preopen') < lines.index('systemctl start sessionquant.service') < lines.index('calendar')
    assert len(runtime_calls(lines)) == 1
    assert not Path(env['HANDOFF']).exists()
    assert '이번 업데이트의 시작 전 정리 확인을 사용합니다: '+status in result.stdout


def test_expired_update_handoff_rechecks_before_runtime(lifecycle):
    result = run_entry(lifecycle, 'update_preopen.sh', 'a'*40, HANDOFF_CHANGE='stale')
    assert result.returncode == 0, result.stderr
    lines = lifecycle[3].read_text().splitlines()
    assert retention_calls(lines) == ['startup-retention ["--backup-before", "UPLOADED previous"]',
                                      'startup-retention []']
    assert lines.index('systemctl start sessionquant.service') < lines.index('startup-retention []') < lines.index('calendar')


@pytest.mark.parametrize('contender', ['start.sh', 'update_preopen.sh', 'stop.sh'])
def test_start_maintenance_excludes_concurrent_lifecycle_writers(lifecycle, contender):
    root, _, _, calls, env = lifecycle
    with subprocess.Popen(['bash', str(root/'deploy/gcp/start.sh')],
                          env={**env, 'BLOCK': 'yes'}, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as active:
        try:
            deadline = time.monotonic()+5
            while not Path(env['READY']).exists() and active.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert Path(env['READY']).exists(), active.communicate(timeout=1)
            before = calls.read_text().splitlines()
            args = ['a'*40] if contender == 'update_preopen.sh' else []
            rejected = run_entry(lifecycle, contender, *args)
            assert rejected.returncode != 0
            assert '다른 시작·업데이트·마감 작업이 진행 중입니다.' in rejected.stderr
            during = calls.read_text().splitlines()[len(before):]
            # Update may fetch/stage its reviewed code before taking the common
            # lock, but none of these entry points may touch Docker or the DB.
            assert all(line.startswith(('git ', 'systemctl is-active --quiet ')) for line in during)
        finally:
            Path(env['RELEASE']).touch()
        stdout, stderr = active.communicate(timeout=10)
    assert active.returncode == 0, stdout+stderr
    assert len(retention_calls(calls.read_text().splitlines())) == 1
