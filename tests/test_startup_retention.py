"""Startup makes decisions from a live probe and this operation's real backup."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


SPEC = importlib.util.spec_from_file_location(
    'startup_retention', Path(__file__).parents[1] / 'deploy/gcp/startup_retention.py')
startup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup)
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def receipt(stamp=NOW, name='current'):
    return f'UPLOADED {stamp.isoformat()} sessionquant-20261125T120000Z-{name}.tar.gz'


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Model only host command boundaries; the helper reads/writes real files."""
    health, etc, app = (tmp_path / name for name in ('health', 'etc', 'app'))
    for directory in (health, etc, app):
        directory.mkdir()
    monkeypatch.setattr(startup, 'ROOT', tmp_path)
    monkeypatch.setattr(startup, 'SUMMARY', health / 'startup-retention-last.json')
    monkeypatch.setattr(startup, 'STATUS', etc / 'startup-retention-status')
    monkeypatch.setattr(startup, 'BACKUP_STATUS', etc / 'backup-status')
    monkeypatch.setattr(startup, 'SEAL', etc / 'nightly-seal.json')
    monkeypatch.setattr(startup, 'utcnow', lambda: NOW)
    ownership = []
    monkeypatch.setattr(startup.os, 'fchown', lambda _fd, uid, gid: ownership.append((uid, gid)))
    authoritative = app / 'retention-last.json'
    authoritative.write_text('{"status":"COMPLETE","authoritative":"unchanged"}\n')
    state = {
        'commands': [], 'probes': ['CLEAR'], 'old': receipt(NOW - timedelta(days=1), 'old'),
        'upload': receipt(), 'backup_exit': 0, 'cleanup_exit': 0, 'writable_exit': 0,
        'app': startup.CommandResult(0), 'ownership': ownership, 'authoritative': authoritative, 'live': [],
    }
    startup.BACKUP_STATUS.write_text(state['old'] + '\n')

    def command(args, *, timeout, output_limit=65536, live=False):
        args = list(args)
        state['commands'].append((args, timeout, output_limit))
        if live:
            state['live'].append(args)
        if args[:3] == ['docker', 'rm', '-f']:
            return startup.CommandResult(0)
        if 'ps' in args:
            return state['app']
        if 'run' in args:
            value = state['probes'].pop(0)
            if isinstance(value, BaseException):
                raise value
            if isinstance(value, startup.CommandResult):
                return value
            reasons = {'CLEAR': 'NO_ELIGIBLE_ROWS', 'DISABLED': 'POLICY_DISABLED',
                       'NEEDS_CLEANUP': 'ELIGIBLE_ROWS', 'UNKNOWN': 'ROW_LIMIT'}
            return startup.CommandResult(0, json.dumps(
                {'read_only': True, 'status': value, 'reason': reasons[value],
                 'namespace': 'private-namespace', 'policy': {'ignored': 'not-for-summary'}}))
        if 'deploy/gcp/backup.sh' in args:
            if state['backup_exit'] == 0 and state['upload'] is not None:
                startup.BACKUP_STATUS.write_text(state['upload'] + '\n')
            return startup.CommandResult(state['backup_exit'])
        if 'deploy/gcp/retention_after_update.sh' in args:
            return startup.CommandResult(state['cleanup_exit'])
        if 'deploy/gcp/nightly_checkpoint.py' in args:
            if not state['writable_exit']:
                startup.SEAL.unlink()
            return startup.CommandResult(state['writable_exit'])
        raise AssertionError(f'Unexpected command: {args}')

    monkeypatch.setattr(startup, 'run_command', command)
    return state


def calls(host, script):
    return [args for args, _timeout, _limit in host['commands'] if script in args]


def test_live_clear_skips_backup_and_preserves_authoritative_report(host):
    previous_report = host['authoritative'].read_bytes()
    code, report = startup.prepare()
    assert code == 0
    assert report['status'] == report['before'] == report['after'] == 'CLEAR'
    assert report['cleanup_attempted'] is report['backup_reused'] is False
    assert not calls(host, 'deploy/gcp/backup.sh')
    assert not calls(host, 'deploy/gcp/retention_after_update.sh')
    assert host['authoritative'].read_bytes() == previous_report
    assert json.loads(startup.SUMMARY.read_text()) == report
    assert startup.SUMMARY.stat().st_mode & 0o777 == 0o644
    assert startup.STATUS.stat().st_mode & 0o777 == 0o600
    assert host['ownership'] == [(1000, 1000), (0, 0)]
    assert 'private-namespace' not in startup.SUMMARY.read_text()
    assert 'UPLOADED' not in startup.STATUS.read_text()
    assert not list(startup.SUMMARY.parent.glob('.*'))
    probe = calls(host, 'run')[0]
    container = probe[probe.index('--name') + 1]
    assert ['docker', 'rm', '-f', container] in calls(host, 'rm')


def test_disabled_policy_skips_backup_and_cleanup(host):
    host['probes'] = ['DISABLED']
    code, report = startup.prepare()
    assert code == 0 and report['status'] == 'DISABLED'
    assert not calls(host, 'deploy/gcp/backup.sh')
    assert not calls(host, 'deploy/gcp/retention_after_update.sh')


def test_eligible_rows_require_new_verified_backup_and_real_before_receipt(host):
    host['probes'] = ['NEEDS_CLEANUP', 'CLEAR']
    code, report = startup.prepare()
    assert code == 0 and report['status'] == 'CLEAR'
    assert report['before'] == 'NEEDS_CLEANUP' and report['cleanup_attempted']
    assert not report['backup_reused']
    backup = calls(host, 'deploy/gcp/backup.sh')[0]
    cleanup = calls(host, 'deploy/gcp/retention_after_update.sh')[0]
    assert backup[:3] == ['timeout', '--kill-after=30', '5400']
    assert cleanup[-1] == host['old']
    assert host['live'] == [backup, cleanup]
    assert host['commands'].index(next(item for item in host['commands'] if item[0] == backup)) < \
        host['commands'].index(next(item for item in host['commands'] if item[0] == cleanup))
    assert len(calls(host, 'run')) == len(calls(host, 'rm')) == 2


def test_update_reuses_fresh_changed_receipt_and_passes_actual_prior_receipt(host):
    host['probes'] = ['NEEDS_CLEANUP', 'CLEAR']
    startup.BACKUP_STATUS.write_text(receipt(NOW - timedelta(minutes=59)) + '\n')
    code, report = startup.prepare(backup_before=host['old'])
    assert code == 0 and report['backup_reused']
    assert not calls(host, 'deploy/gcp/backup.sh')
    assert calls(host, 'deploy/gcp/retention_after_update.sh')[0][-1] == host['old']


def test_normal_start_does_not_reuse_an_unrelated_recent_backup(host):
    host['probes'] = ['NEEDS_CLEANUP', 'CLEAR']
    recent = receipt(NOW - timedelta(seconds=1), 'unrelated')
    startup.BACKUP_STATUS.write_text(recent + '\n')
    code, report = startup.prepare()
    assert code == 0 and not report['backup_reused']
    assert calls(host, 'deploy/gcp/backup.sh')
    assert calls(host, 'deploy/gcp/retention_after_update.sh')[0][-1] == recent


@pytest.mark.parametrize('current', [
    receipt(NOW - timedelta(seconds=3601)), receipt(NOW + timedelta(seconds=1)),
    'LOCAL_ONLY 2026-11-25T12:00:00Z sessionquant-20261125T120000Z-local.tar.gz',
    'UPLOADED 2026-11-25T12:00:00 sessionquant-20261125T120000Z-naive.tar.gz',
    'UPLOADED 2026-11-25T12:00:00Z unsafe-name', 'broken receipt with actual spaces  ',
])
def test_stale_future_or_invalid_update_receipt_forces_backup(host, current):
    host['probes'] = ['NEEDS_CLEANUP', 'CLEAR']
    startup.BACKUP_STATUS.write_text(current + '\n')
    code, report = startup.prepare(backup_before=host['old'])
    assert code == 0 and not report['backup_reused']
    assert calls(host, 'deploy/gcp/backup.sh')
    assert calls(host, 'deploy/gcp/retention_after_update.sh')[0][-1] == current


def test_unchanged_fresh_update_receipt_cannot_authorize_reuse(host):
    host['probes'] = ['NEEDS_CLEANUP', 'CLEAR']
    current = receipt(NOW - timedelta(minutes=1), 'prior-operation')
    startup.BACKUP_STATUS.write_text(current)
    code, report = startup.prepare(backup_before=current)
    assert code == 0 and not report['backup_reused']
    assert calls(host, 'deploy/gcp/backup.sh')


def test_failed_backup_aborts_without_unsealing_or_retention(host):
    host['probes'] = ['NEEDS_CLEANUP']
    host['backup_exit'] = 124
    startup.SEAL.write_text('{"version":1}')
    code, report = startup.prepare()
    assert code == 1 and report['status'] == 'FAILED' and report['reason'] == 'BACKUP_FAILED'
    assert not report['cleanup_attempted']
    assert startup.SEAL.exists()
    assert not calls(host, 'deploy/gcp/retention_after_update.sh')
    assert not calls(host, 'deploy/gcp/nightly_checkpoint.py')


@pytest.mark.parametrize('uploaded', [None, 'FAILED verification', receipt(NOW - timedelta(minutes=1))])
def test_success_exit_without_current_operation_upload_does_not_delete(host, uploaded):
    host['probes'] = ['NEEDS_CLEANUP']
    host['upload'] = uploaded
    code, report = startup.prepare()
    assert code == 1 and report['reason'] == 'BACKUP_NOT_VERIFIED'
    assert not calls(host, 'deploy/gcp/retention_after_update.sh')


@pytest.mark.parametrize(('after', 'status'), [('NEEDS_CLEANUP', 'PENDING'), ('UNKNOWN', 'UNKNOWN')])
def test_bounded_cleanup_can_allow_start_without_falsely_claiming_clear(host, after, status):
    host['probes'] = ['NEEDS_CLEANUP', after]
    code, report = startup.prepare()
    assert code == 0 and report['status'] == status and report['after'] == after
    assert report['cleanup_attempted']
    assert json.loads(host['authoritative'].read_text())['status'] == 'COMPLETE'


@pytest.mark.parametrize('failed_probe', [
    startup.CommandResult(1, 'credentials must not reach summary'),
    startup.CommandResult(-9, '', timed_out=True),
    startup.CommandResult(0, '{"status":"CLEAR","read_only":true}', truncated=True),
    startup.CommandResult(0, '{"status":"CLEAR"}'),
    startup.CommandResult(0, 'invalid json'), OSError('Docker unavailable'),
])
def test_probe_errors_are_unknown_and_still_require_backup_and_protected_cleanup(host, failed_probe):
    host['probes'] = [failed_probe, 'UNKNOWN']
    code, report = startup.prepare()
    assert code == 0 and report['before'] == report['status'] == 'UNKNOWN'
    assert calls(host, 'deploy/gcp/backup.sh')
    assert calls(host, 'deploy/gcp/retention_after_update.sh')
    assert len(calls(host, 'rm')) == 2
    assert 'credentials' not in startup.SUMMARY.read_text()


@pytest.mark.parametrize('app_state', [startup.CommandResult(0, 'app\n'), startup.CommandResult(1),
                                      startup.CommandResult(0, '', timed_out=True)])
def test_running_or_uncheckable_app_refuses_probe_backup_and_delete(host, app_state):
    host['app'] = app_state
    code, report = startup.prepare()
    assert code == 1 and report['status'] == 'FAILED'
    assert not calls(host, 'run')
    assert not calls(host, 'deploy/gcp/backup.sh')
    assert not calls(host, 'deploy/gcp/retention_after_update.sh')


def test_cleanup_error_aborts_and_does_not_reuse_initial_probe_as_after_state(host):
    host['probes'] = ['NEEDS_CLEANUP']
    host['cleanup_exit'] = 1
    code, report = startup.prepare()
    assert code == 1 and report['status'] == 'FAILED' and report['reason'] == 'CLEANUP_FAILED'
    assert report['after'] == 'UNKNOWN' and report['cleanup_attempted']


def test_clear_sealed_database_keeps_checkpoint_and_app_files_untouched(host):
    startup.SEAL.write_text('{"version":1}')
    previous_report = host['authoritative'].read_bytes()
    code, report = startup.prepare()
    assert code == 0 and report['status'] == 'CLEAR'
    assert startup.SEAL.exists()
    assert host['authoritative'].read_bytes() == previous_report
    assert startup.SUMMARY.parent.name == 'health'
    assert not calls(host, 'deploy/gcp/nightly_checkpoint.py')


def test_first_clear_start_creates_missing_health_directory(host):
    startup.SUMMARY.parent.rmdir()
    code, report = startup.prepare()
    assert code == 0 and report['status'] == 'CLEAR'
    assert startup.SUMMARY.is_file()
    assert startup.SUMMARY.parent.stat().st_mode & 0o777 == 0o755


def test_sealed_cleanup_backs_up_before_unsealing_then_deletes(host):
    host['probes'] = ['NEEDS_CLEANUP', 'CLEAR']
    startup.SEAL.write_text('{"version":1}')
    code, report = startup.prepare()
    assert code == 0 and report['cleanup_attempted'] and not startup.SEAL.exists()
    operations = [arg for args, _, _ in host['commands'] for arg in args if arg in {
        'deploy/gcp/backup.sh', 'deploy/gcp/nightly_checkpoint.py', 'deploy/gcp/retention_after_update.sh'}]
    assert operations == ['deploy/gcp/backup.sh', 'deploy/gcp/nightly_checkpoint.py',
                          'deploy/gcp/retention_after_update.sh']


def test_unseal_failure_aborts_before_deletion(host):
    host['probes'] = ['NEEDS_CLEANUP']
    host['writable_exit'] = 1
    startup.SEAL.write_text('{"version":1}')
    code, report = startup.prepare()
    assert code == 1 and report['reason'] == 'UNSEAL_FAILED'
    assert not report['cleanup_attempted']
    assert not calls(host, 'deploy/gcp/retention_after_update.sh')


def test_probe_interruption_removes_exact_named_container_and_records_failure(host):
    host['probes'] = [InterruptedError()]
    code, report = startup.prepare()
    assert code == 1 and report['reason'] == 'INTERRUPTED'
    command = calls(host, 'run')[0]
    container = command[command.index('--name') + 1]
    assert calls(host, 'rm') == [['docker', 'rm', '-f', container]]
    assert not calls(host, 'deploy/gcp/backup.sh')


def test_runner_keeps_json_stdout_separate_from_compose_stderr_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, 'ROOT', tmp_path)
    command = [sys.executable, '-c',
               'import sys; print("container created", file=sys.stderr); print("{\\"status\\":\\"CLEAR\\"}")']
    result = startup.run_command(command, timeout=5)
    assert result.returncode == 0 and json.loads(result.stdout) == {'status': 'CLEAR'}


def test_runner_bounds_output_and_kills_a_hung_process(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, 'ROOT', tmp_path)
    noisy = startup.run_command([sys.executable, '-c', 'print("x" * 2000000)'], timeout=5, output_limit=256)
    assert noisy.returncode == 0 and noisy.truncated and len(noisy.stdout) == 256
    started = time.monotonic()
    hung = startup.run_command([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=0.1)
    assert hung.timed_out and hung.returncode != 0
    assert time.monotonic() - started < 5


def test_operator_interrupt_allows_child_cleanup_trap(tmp_path):
    ready, cleaned = tmp_path/'ready', tmp_path/'cleaned'
    child = tmp_path/'child.sh'
    child.write_text('trap \'echo cleaned > "$2"; exit 143\' TERM\n'
                     'touch "$1"\nwhile true; do sleep 1; done\n')
    harness = tmp_path/'harness.py'
    harness.write_text('''import importlib.util, signal, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('helper',sys.argv[1])
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
helper.ROOT=Path(sys.argv[2]).parent
def interrupt(*args):raise KeyboardInterrupt
signal.signal(signal.SIGTERM,interrupt)
try:helper.run_command(['bash',*sys.argv[2:]],timeout=60,live=True)
except KeyboardInterrupt:pass
''')
    with subprocess.Popen([sys.executable, str(harness), str(SPEC.origin), str(child),
                           str(ready), str(cleaned)], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL) as process:
        try:
            deadline = time.monotonic()+5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists()
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=5) == 0
            assert cleaned.read_text().strip() == 'cleaned'
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
