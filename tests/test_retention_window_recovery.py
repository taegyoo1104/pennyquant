from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

DEPLOY = Path(__file__).parents[1] / 'deploy/gcp'


def module(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


window = module('retention_window')
recovery = module('resume_start_after_retention_expiry')
UPLOADED = datetime(2026, 11, 25, 10, 43, 35, tzinfo=timezone.utc)
NAME = 'sessionquant-20261125T102039Z-fixture.tar.gz'
RECEIPT = f'UPLOADED {UPLOADED.isoformat()} {NAME}'


@pytest.mark.parametrize('age,allowed', [(0, True), (3450, True), (3451, False), (3599, False), (3601, False)])
def test_window_defers_without_extending_receipt(age, allowed):
    assert window.has_window(RECEIPT, 'old', UPLOADED + timedelta(seconds=age)) is allowed


@pytest.mark.parametrize('current,previous', [('FAILED old', 'old'), (RECEIPT, RECEIPT),
    (f'UPLOADED 2026-11-25T10:43:35 {NAME}', 'old')])
def test_invalid_receipt_never_authorizes_window(current, previous):
    with pytest.raises(ValueError):
        window.has_window(current, previous, UPLOADED)


def test_future_receipt_rejected():
    with pytest.raises(ValueError):
        window.has_window(RECEIPT, 'old', UPLOADED - timedelta(seconds=1))


@pytest.mark.parametrize('nightly,exit_code', [(False, 0), (True, 2)])
def test_single_continuous_worker_preserves_partial_report(tmp_path, nightly, exit_code):
    passes = 1
    deploy = tmp_path / 'deploy/gcp'
    deploy.mkdir(parents=True)
    report = tmp_path / 'retention-last.json'
    original = {'status': 'PARTIAL', 'dry_run': False, 'errors': [], 'observability': {'backlog_status': 'UNKNOWN'}}
    report.write_text(json.dumps(original))
    calls = tmp_path / 'calls'
    (deploy / 'retention_window.py').write_text(f'''from pathlib import Path
p=Path({str(calls)!r})
count=len(p.read_text().splitlines()) if p.exists() else 0
raise SystemExit(3 if count >= {passes} else 0)
''')
    (deploy / 'retention.sh').write_text(f'echo pass >> "{calls}"\n')
    script = tmp_path / 'catchup.sh'
    script.write_text((DEPLOY / 'retention_after_update.sh').read_text()
                      .replace('/opt/sessionquant', str(tmp_path))
                      .replace('/var/lib/sessionquant/app/retention-last.json', str(report)))
    result = subprocess.run(['bash', str(script), 'old', *(['--nightly'] if nightly else [])],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == exit_code, result.stderr
    assert (len(calls.read_text().splitlines()) if calls.exists() else 0) == passes
    assert json.loads(report.read_text()) == original


def evidence():
    started = UPLOADED + timedelta(minutes=59)
    finished = started + timedelta(seconds=90)
    report = {'dry_run': False, 'status': 'PARTIAL', 'errors': [], 'backup': NAME,
              'started_at': started.isoformat(), 'finished_at': finished.isoformat(),
              'tables': {'research_events': {'error': None}}, 'namespace': 'fixture'}
    failure = {'checked_at': (finished + timedelta(seconds=2)).isoformat(), 'status': 'FAILED',
               'reason': 'CLEANUP_FAILED', 'cleanup_attempted': True}
    return report, failure


def test_recovery_requires_matching_successful_partial_pass():
    report, failure = evidence()
    assert recovery.validate_evidence(RECEIPT, report, failure, UPLOADED + timedelta(hours=2)) == UPLOADED


@pytest.mark.parametrize('field,value', [('dry_run', True), ('status', 'FAILED'), ('status', 'COMPLETE'),
    ('errors', [{'code': 3024}]), ('backup', 'other'), ('tables', {'x': {'error': {'code': 3024}}}),
    ('started_at', (UPLOADED + timedelta(hours=1, seconds=1)).isoformat()),
    ('finished_at', (UPLOADED + timedelta(hours=2)).isoformat())])
def test_recovery_rejects_unverified_or_failed_deletion(field, value):
    report, failure = evidence()
    report[field] = value
    with pytest.raises(ValueError):
        recovery.validate_evidence(RECEIPT, report, failure, UPLOADED + timedelta(hours=2))


@pytest.mark.parametrize('age', [-1, 3599, 21601])
def test_recovery_is_not_general_backup_reuse(age):
    with pytest.raises(ValueError):
        recovery.validate_evidence(RECEIPT, *evidence(), UPLOADED + timedelta(seconds=age))


@pytest.mark.parametrize('field,value', [('status', 'PENDING'), ('reason', 'BACKUP_FAILED'),
    ('cleanup_attempted', False), ('checked_at', UPLOADED.isoformat())])
def test_recovery_rejects_other_startup_failures(field, value):
    report, failure = evidence()
    failure[field] = value
    with pytest.raises(ValueError):
        recovery.validate_evidence(RECEIPT, report, failure, UPLOADED + timedelta(hours=2))


@pytest.mark.parametrize('patch', [{'mode': 'live'}, {'status': 'RUNNING'}, {'running': True}, {'armed': True}, {'armed': 0}])
def test_control_must_be_offline_and_unarmed(patch):
    row = {'mode': 'paper', 'status': 'OFFLINE', 'running': False, 'armed': False, **patch}
    with pytest.raises(ValueError):
        recovery.validate_control((row, UPLOADED), UPLOADED)


def test_control_changed_after_backup_rejected():
    row = {'mode': 'paper', 'status': 'OFFLINE', 'running': False, 'armed': False}
    recovery.validate_control((row, UPLOADED.replace(tzinfo=None)), UPLOADED)
    with pytest.raises(ValueError):
        recovery.validate_control((row, UPLOADED + timedelta(seconds=1)), UPLOADED)


@pytest.mark.parametrize('probe,success', [('NEEDS_CLEANUP', True), ('CLEAR', True), ('UNKNOWN', False)])
def test_host_recovery_only_hands_off_after_read_only_checks(tmp_path, monkeypatch, probe, success):
    host = module('resume_start_after_retention_expiry')
    root, etc = tmp_path / 'root', tmp_path / 'etc'
    root.mkdir(); etc.mkdir()
    report, failure = evidence()
    paths = {'ROOT': root, 'ETC': etc, 'REPORT': tmp_path / 'report',
             'SUMMARY': tmp_path / 'summary', 'MARKER': tmp_path / 'marker'}
    for key, value in paths.items(): monkeypatch.setattr(host, key, value)
    paths['REPORT'].write_text(json.dumps(report)); paths['SUMMARY'].write_text(json.dumps(failure))
    (etc / 'backup-status').write_text(RECEIPT); (etc / 'retention-status').write_text('FAILED expiry')
    monkeypatch.setattr(host.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(host.socket, 'gethostname', lambda: 'sessionquant-paper')
    monkeypatch.setattr(host, 'lock', lambda *a: None)
    monkeypatch.setattr(host.fcntl, 'flock', lambda *a: None)
    monkeypatch.setattr(host.os, 'close', lambda *a: None)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return UPLOADED + timedelta(hours=2)
    monkeypatch.setattr(host, 'datetime', Clock)
    deploy = root / 'deploy/gcp'; deploy.mkdir(parents=True)
    (deploy / 'startup_retention.py').write_text(f'''import json
from pathlib import Path
def probe(): return {probe!r}, 'fixture'
def persist(value): Path({str(paths['SUMMARY'])!r}).write_text(json.dumps(value))
''')
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        if args[:2] == ['systemctl', 'show']: return 'inactive'
        if args[:2] == ['git', 'rev-parse']: return host.REVISION
        if 'ps' in args: return ''
        if args == ['python3', 'deploy/gcp/startup_handoff.py', 'create']:
            summary = json.loads(paths['SUMMARY'].read_text())
            assert summary['previous_failure'] == failure
            assert summary['cleanup_attempted'] is False
    monkeypatch.setattr(host, 'run', run)
    if success:
        host.recover()
    else:
        with pytest.raises(ValueError): host.recover()
    assert (['systemctl', 'start', 'sessionquant.service'] in commands) is success
    assert sum('--verify' in args for args in commands) == (2 if success else 1)
    assert not any('backup.sh' in ' '.join(args) or 'retention.sh' in ' '.join(args) for args in commands)
    assert (etc / 'backup-status').read_text() == RECEIPT
    assert json.loads(paths['REPORT'].read_text()) == report
