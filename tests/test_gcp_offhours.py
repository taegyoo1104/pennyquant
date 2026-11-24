"""Off-hours calendar, ledger and host orchestration without cloud mutations."""
from datetime import datetime
import os
import subprocess

import pytest

from test_gcp_deployment import DEPLOY, executable
from test_paused_update_guard import guard


@pytest.mark.parametrize('stamp,allowed', [
    ('2026-11-21T16:00:00+00:00', True),
    ('2026-11-20T21:00:00+00:00', True),
    ('2026-11-27T18:00:00+00:00', True),
    ('2026-11-20T14:29:00+00:00', False),
    ('2026-11-20T14:30:00+00:00', False),
    ('2026-11-20T20:59:59+00:00', False),
])
def test_offhours_update_and_startup_use_actual_close(stamp, allowed):
    from sessionquant.market.bars import session_bounds
    from sessionquant.market_shutdown import startup_delay
    now = datetime.fromisoformat(stamp)
    if allowed: guard().verify_window(now, session_bounds(now), off_hours=True)
    else:
        with pytest.raises(ValueError): guard().verify_window(now, session_bounds(now), off_hours=True)
    assert (startup_delay(now) == 0) is allowed


@pytest.mark.parametrize('running', ['none', 'inspection', 'engine'])
def test_inspect_refuses_to_replace_running_trading_engine(tmp_path, running):
    root = tmp_path/'repo'
    root.mkdir()
    bindir = tmp_path/'bin'; bindir.mkdir()
    calls = tmp_path/'calls'
    executable(bindir/'docker', '''echo "docker $*" >> "$CALLS"
if [[ "$*" == *"--services app"* && "$MODE" != none ]]; then echo app; fi
if [[ "$1" == inspect ]]; then
    if [[ "$MODE" == inspection ]]; then echo '["python","-m","sessionquant.inspection_dashboard"]'
    else echo '["sh","-c","exec python -m sessionquant run"]'; fi
fi
''')
    executable(bindir/'systemctl', 'echo "systemctl $*" >> "$CALLS"\n')
    executable(bindir/'python3', 'exit 0\n')
    script = tmp_path/'inspect.sh'
    script.write_text((DEPLOY/'inspect.sh').read_text().replace('/opt/sessionquant', str(root))
                      .replace('/etc/sessionquant', str(tmp_path/'etc')))
    result = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'PATH': str(bindir)+':'+os.environ['PATH'],
                                 'CALLS': str(calls), 'MODE': running})
    assert (result.returncode == 0) is (running != 'engine')
    lines = calls.read_text()
    assert ('compose.inspection.yaml up' in lines) is (running != 'engine')
    assert 'poweroff' not in lines and 'stop -t' not in lines


def test_offhours_guard_owns_same_lease_as_engine_and_reads_only(monkeypatch):
    import json
    import sys
    import pymysql
    from hashlib import sha256
    from types import SimpleNamespace
    from sessionquant.database.config import MySQLConfig, namespace_for
    import sessionquant.market.bars as bars
    import sessionquant.session_config as config_module
    module = guard()
    monkeypatch.setattr(bars, 'session_bounds', lambda now: None)
    cfg = SimpleNamespace(environment='PAPER_LOCAL', database_backend='mysql', db_path='data/paper.db')
    monkeypatch.setattr(config_module, 'load_session_config', lambda path: cfg)
    db = SimpleNamespace(environment='PAPER_LOCAL', namespace='test-paper', database='sessionquant',
                         host='localhost', port=3306, user='test', password='private')
    monkeypatch.setattr(MySQLConfig, 'from_env', lambda: db)
    monkeypatch.setattr(sys, 'argv', ['guard.py', '--off-hours'])
    queries = []
    control = {'mode': 'paper', 'running': False, 'armed': False, 'status': 'OFFLINE', 'run_id': 'last'}
    state = {'environment': 'PAPER_LOCAL', 'broker': {'orders': {}, 'positions': {}, 'exit_intents': {}}}
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params=None): queries.append((query, params))
        def fetchone(self):
            query = queries[-1][0]
            if 'GET_LOCK' in query: return (1,)
            if 'pq_controls' in query: return (json.dumps(control),)
            if 'pq_engine_state' in query: return (json.dumps(state),)
            return (0,)
    closed = []
    monkeypatch.setattr(pymysql, 'connect', lambda **kwargs: SimpleNamespace(
        cursor=Cursor, close=lambda: closed.append(True)))
    module.main()
    ns = namespace_for(cfg.db_path, db.namespace, cfg.environment)
    lock = 'pennyquant:' + sha256(f'{db.database}:{ns}'.encode()).hexdigest()[:48]
    assert queries[0] == ('SELECT GET_LOCK(%s,0)', (lock,))
    assert queries[1][0] == 'START TRANSACTION READ ONLY' and closed == [True]
    assert all(q.startswith(('SELECT', 'START TRANSACTION READ ONLY')) for q, _ in queries)
    control['armed'] = True
    with pytest.raises(ValueError): module.main()
    assert len(closed) == 2


@pytest.mark.parametrize('wrong_device', [False, True])
@pytest.mark.parametrize('identity', ['hostname', 'missing_hostname', 'wrong_hostname', 'hostname_symlink',
                                      'missing_config', 'wrong_service', 'recovery_mask', 'maintenance'])
def test_offline_boot_preparation_only_disables_units_on_expected_detached_disk(tmp_path, wrong_device, identity):
    root = tmp_path/'disk'
    (root/'etc/sessionquant').mkdir(parents=True)
    (root/'etc/systemd/system').mkdir(parents=True)
    (root/'opt/sessionquant/deploy/gcp').mkdir(parents=True)
    (root/'opt/sessionquant/deploy/gcp/compose.yaml').touch()
    if identity != 'missing_hostname':
        (root/'etc/hostname').write_text('sessionquant-paper.us-central1-a.c.sessionquant.internal\n')
    for unit in ('sessionquant.service', 'sessionquant-cost-guard.timer'):
        (root/'etc/systemd/system'/unit).write_text('[Unit]\nDescription=fixture\n')
    service = root/'etc/systemd/system/sessionquant.service'
    service.write_text('[Service]\nExecStart=/opt/sessionquant/deploy/gcp/start.sh\n')
    for config in ('app.env', 'db.env'):
        (root/'etc/sessionquant'/config).write_text('SECRET=preserved\n')
    if identity == 'wrong_hostname': (root/'etc/hostname').write_text('another-vm\n')
    if identity == 'hostname_symlink':
        (root/'etc/hostname').unlink()
        (root/'etc/hostname').symlink_to('/does-not-exist')
    if identity == 'missing_config': (root/'etc/sessionquant/db.env').unlink()
    if identity == 'wrong_service': service.write_text('[Service]\nExecStart=/bin/true\n')
    if identity == 'recovery_mask':
        service.unlink()
        service.symlink_to('/dev/null')
    if identity == 'maintenance': (root/'etc/sessionquant/maintenance').touch()
    data = root/'db'; data.write_bytes(b'preserve')
    bindir = tmp_path/'bin'; bindir.mkdir(); calls = tmp_path/'calls'
    executable(bindir/'mountpoint', 'exit 0\n')
    executable(bindir/'findmnt', '''if [[ "$*" == *OPTIONS* ]]; then echo rw;
elif [[ "$*" == *--mountpoint* ]]; then echo /dev/operating1; else echo /dev/helper1; fi
''')
    executable(bindir/'readlink', '''if [[ "$2" == /dev/disk/by-id/google-sq-operating-part1 ]]; then
    if [[ "$WRONG" == True ]]; then echo /dev/wrong1; else echo /dev/operating1; fi
else echo "$2"; fi
''')
    executable(bindir/'systemctl', '''echo "systemctl $*" >> "$CALLS"
if [[ "$*" == *is-enabled* ]]; then echo disabled; exit 1; fi
''')
    executable(bindir/'sync', 'echo sync >> "$CALLS"\n')
    # The tool requires root on GCE; all system operations here use test doubles.
    script = tmp_path/'offline.sh'
    script.write_text((DEPLOY/'offline_inspection_boot.sh').read_text().replace('$EUID == 0', '1 == 1'))
    result = subprocess.run(['bash', str(script), str(root)], capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'PATH': str(bindir)+':'+os.environ['PATH'],
                                 'CALLS': str(calls), 'WRONG': str(wrong_device)})
    allowed = not wrong_device and identity in {'hostname', 'missing_hostname'}
    assert (result.returncode == 0) is allowed, result.stderr
    assert data.read_bytes() == b'preserve'
    assert (root/'etc/sessionquant/app.env').read_text() == 'SECRET=preserved\n'
    if identity == 'missing_hostname': assert not (root/'etc/hostname').exists()
    if not allowed: assert not calls.exists()
    else:
        output = calls.read_text()
        assert 'disable sessionquant.service sessionquant-cost-guard.timer' in output
        assert ' start ' not in output and 'poweroff' not in output


@pytest.mark.parametrize('checkpoint', ['absent', 'valid', 'invalid'])
@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('failure', ['', 'engine', 'guard', 'backup', 'retention', 'resume_guard', 'receipt_changed'])
def test_offhours_update_backs_up_before_install_and_never_starts_engine(tmp_path, failure, resume, checkpoint):
    if (resume and checkpoint != "absent") or (checkpoint == "valid" and failure == "backup"):
        pytest.skip("inapplicable backup path")
    if (resume and failure == 'backup') or (not resume and failure in {'resume_guard', 'receipt_changed', 'retention'}):
        pytest.skip('not applicable to this update mode')
    root = tmp_path/'repo'; (root/'deploy/gcp').mkdir(parents=True)
    stage = tmp_path/'archive'; deploy = stage/'deploy/gcp'; deploy.mkdir(parents=True)
    etc = tmp_path/'etc'; etc.mkdir(); (etc/'backup-status').write_text('UPLOADED previous')
    (etc/'retention-status').write_text('FAILED fixture')
    if checkpoint != 'absent':
        (etc/'nightly-checkpoint.json').write_text('{}')
        (etc/'nightly-seal.json').write_text('{}')
    (deploy/'nightly_checkpoint.py').write_text("""import os,sys
with open(os.environ['CALLS'],'a') as f:f.write('checkpoint-'+sys.argv[1]+'\\n')
if sys.argv[1]=='verify' and os.environ['CHECKPOINT']=='invalid':raise SystemExit(1)
""")
    bindir = tmp_path/'bin'; bindir.mkdir(); calls = tmp_path/'calls'
    (deploy/'prepare_update.py').write_text('''import os,sys,shutil
from pathlib import Path
with open(os.environ['CALLS'],'a') as f: f.write('install-code\\n' if '--backup' in sys.argv else 'check-code\\n')
if '--backup' in sys.argv: shutil.copytree(Path(__file__).parents[2],sys.argv[sys.argv.index('--repo')+1],dirs_exist_ok=True)
''')
    (deploy/'storage_check.py').write_text('pass\n')
    (deploy/'update_guard.py').write_text('# the actual guard has separate DB/lease tests\n')
    (deploy/'resume_retention_guard.py').write_text('# receipt age and offline heartbeat tested separately\n')
    (deploy/'sessionquant.service').write_text('[Unit]\nDescription=test\n')
    (deploy/'sessionquant-storage-guard.timer').write_text('[Unit]\nDescription=test timer\n')
    (etc/'systemd/system').mkdir(parents=True)
    for name, body in {
        'backup.sh': 'echo backup >> "$CALLS"\n[[ "$FAILURE" != backup ]] || exit 7\nprintf "UPLOADED fresh\\n" > "$RECEIPT"\n',
        'retention_after_update.sh': 'echo retention >> "$CALLS"\n[[ "$FAILURE" != retention ]]\n',
        'preopen_check.sh': 'echo "preopen $*" >> "$CALLS"\n[[ "$1" == --offline ]]\n',
        'inspect.sh': 'echo inspection >> "$CALLS"\n',
    }.items():
        executable(deploy/name, body)
        executable(root/'deploy/gcp'/name, body)
    executable(bindir/'git', '''echo "git $*" >> "$CALLS"
if [[ "$1" == rev-parse ]]; then printf '%040d\\n' 1
elif [[ "$1" == archive ]]; then tar -cf - -C "$ARCHIVE" .; fi
''')
    executable(bindir/'docker', '''echo "docker $*" >> "$CALLS"
if [[ "$*" == *"--services app"* && "$FAILURE" == engine ]]; then echo app; fi
if [[ "$1" == inspect ]]; then echo '["sh","-c","exec python -m sessionquant run"]'; fi
if [[ "$*" == *" python - --off-hours"* ]]; then cat >/dev/null; [[ "$FAILURE" != guard ]] || exit 7; fi
if [[ "$*" == *" python - 0000000000000000000000000000000000000001 "* ]]; then
    cat >/dev/null; echo resume-check >> "$CALLS"; [[ "$FAILURE" != resume_guard ]] || exit 7
fi
if [[ "$1" == build && "$FAILURE" == receipt_changed ]]; then echo 'UPLOADED changed' > "$RECEIPT"; fi
if [[ "$*" == *"SELECT @@GLOBAL.log_bin"* ]]; then echo 0; fi
''')
    executable(bindir/'systemctl', '''echo "systemctl $*" >> "$CALLS"
if [[ "$1" == is-active ]]; then exit 3; fi
if [[ "$1" == is-enabled ]]; then echo disabled; fi
''')
    script = tmp_path/'update.sh'
    body = (DEPLOY/'update_preopen.sh').read_text()
    # Constrain every production path to this disposable fixture.
    body = body.replace('$EUID == 0', '1 == 1')
    host_check = next(line for line in body.splitlines() if '/sys/class/dmi' in line)
    body = body.replace(host_check, ': # GCE identity checked by the production wrapper')
    body = body.replace('/opt/sessionquant', str(root)).replace('/etc/sessionquant', str(etc))
    body = body.replace('/etc/systemd/system', str(etc/'systemd/system'))
    body = body.replace('/run/sessionquant-update.lock', str(tmp_path/'lock'))
    body = body.replace('/var/tmp/sessionquant-update.', str(tmp_path/'work.'))
    body = body.replace('/var/lib/sessionquant', str(tmp_path/'data'))
    script.write_text(body)
    result = subprocess.run(['bash', str(script), 'a'*40, '--resume-off-hours' if resume else '--off-hours'], capture_output=True,
                            text=True, timeout=15, env={**os.environ,
                            'PATH': str(bindir)+':'+os.environ['PATH'], 'CALLS': str(calls),
                            'ARCHIVE': str(stage), 'RECEIPT': str(etc/'backup-status'), 'FAILURE': failure, 'CHECKPOINT': checkpoint})
    assert (result.returncode == 0) is (not failure), result.stderr
    lines = calls.read_text().splitlines()
    assert 'systemctl start sessionquant.service' not in lines
    assert not any('check-toss' in line or 'poweroff' in line for line in lines)
    if not failure:
        if resume:
            assert lines.index('install-code') < lines.index('retention')
            assert 'backup' not in lines and lines.count('resume-check') == 3
        elif checkpoint == 'valid':
            assert 'backup' not in lines and lines.count('checkpoint-verify') == 2
            assert lines.index('install-code') < lines.index('checkpoint-writable') < lines.index('preopen --offline')
        else:
            assert lines.index('backup') < lines.index('install-code')
        if resume: assert lines.index('retention') < lines.index('preopen --offline')
        else: assert 'retention' not in lines
        assert lines.index('preopen --offline') < lines.index('inspection')
    else:
        assert 'inspection' not in lines
        if failure == 'retention':
            assert lines.index('install-code') < lines.index('retention')
            assert ('backup' in lines) is (not resume)
        else:
            assert 'install-code' not in lines and 'retention' not in lines
            assert ('backup' in lines) is (failure == 'backup')
