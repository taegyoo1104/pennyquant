"""Nightly backup reuse rejects unknown/changed data without reading all DB rows."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import importlib.util
import json

import pytest

from test_gcp_deployment import DEPLOY


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('nightly_checkpoint', DEPLOY/'nightly_checkpoint.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    etc, data = tmp_path/'etc', tmp_path/'data'
    etc.mkdir(); (data/'app').mkdir(parents=True); (data/'backups').mkdir()
    for name in ('app.env', 'db.env', 'backup-bucket'):
        (etc/name).write_text('private fixture')
    (data/'app/retention-last.json').write_text(json.dumps({'status':'COMPLETE', 'dry_run':False,
        'errors':[], 'observability':{'backlog_status':'UNKNOWN'}}))
    for key, value in {'ETC':etc, 'DATA':data, 'PROOF':etc/'nightly-checkpoint.json',
                       'SEAL':etc/'nightly-seal.json'}.items():
        monkeypatch.setattr(m, key, value)
    state = {'container_started':'one', 'server_uuid':'uuid', 'readonly':False,
             'writes':{'Com_insert':5, 'Com_update':3}, 'boot_epoch':1000., 'flush_epoch':1000.}
    monkeypatch.setattr(m, 'snapshot', lambda:deepcopy(state))
    monkeypatch.setattr(m, 'stopped', lambda:None)
    def sql(query):
        if query.startswith('SELECT'):return str(int(state['readonly']))
        state['readonly'] = 'super_read_only=ON' in query
        return ''
    monkeypatch.setattr(m, 'sql', sql)
    def backup(number):
        name = f'sessionquant-20261124T210000Z-fixture-{number}.tar.gz'
        (data/'backups'/name).write_bytes(b'verified uploaded archive')
        stamp = datetime.now(timezone.utc).isoformat()
        (etc/'backup-status').write_text(f'UPLOADED {stamp} {name}')
    backup(1)
    return m, state, backup


def completed(checkpoint):
    m, state, backup = checkpoint
    m.seal(); backup(2); m.record()
    return m, state, backup


def test_reuse_after_cleanup_and_final_backup_until_runtime_opens(checkpoint):
    m, state, _ = completed(checkpoint)
    m.verify()
    assert state['readonly'] and m.PROOF.stat().st_mode & 0o777 == 0o600
    m.writable()
    assert not m.PROOF.exists() and not m.SEAL.exists() and not state['readonly']
    with pytest.raises(FileNotFoundError):m.verify()


@pytest.mark.parametrize('change', ['write', 'restart', 'flush', 'readonly', 'file', 'new_file',
    'configuration', 'archive', 'missing_archive', 'receipt', 'expired', 'seal'])
def test_changed_or_unverifiable_snapshot_never_reuses_backup(checkpoint, change):
    m, state, backup = completed(checkpoint)
    if change == 'write':state['writes']['Com_update'] += 1
    elif change == 'restart':state['container_started'] = 'two'
    elif change == 'flush':state['flush_epoch'] += 60
    elif change == 'readonly':state['readonly'] = False
    elif change == 'file':(m.DATA/'app/retention-last.json').write_text('{}')
    elif change == 'new_file':(m.DATA/'app/new-record').write_text('changed')
    elif change == 'configuration':(m.ETC/'app.env').write_text('changed')
    elif change == 'archive':next((m.DATA/'backups').glob('*fixture-2*')).write_bytes(b'changed')
    elif change == 'missing_archive':next((m.DATA/'backups').glob('*fixture-2*')).unlink()
    elif change == 'receipt':backup(3)
    elif change == 'seal':m.SEAL.unlink()
    elif change == 'expired':
        name = (m.ETC/'backup-status').read_text().split()[-1]
        old = (datetime.now(timezone.utc)-timedelta(hours=97)).isoformat()
        (m.ETC/'backup-status').write_text(f'UPLOADED {old} {name}')
    with pytest.raises((ValueError, FileNotFoundError)):m.verify()


def test_old_backup_and_mid_backup_mutations_cannot_create_proof(checkpoint):
    m, state, backup = checkpoint
    m.seal()
    with pytest.raises(ValueError, match='새 최종 백업'):m.record()
    assert not m.PROOF.exists()
    backup(2);state['writes']['Com_update'] += 1
    with pytest.raises(ValueError, match='백업 도중'):m.record()
    assert not m.PROOF.exists()


@pytest.mark.parametrize('status,errors,backlog', [('PARTIAL', [], 'UNKNOWN'),
    ('COMPLETE', [{'code':3024}], 'CLEAR'), ('COMPLETE', [], 'PRESENT')])
def test_incomplete_cleanup_never_publishes_checkpoint(checkpoint, status, errors, backlog):
    m, _, backup = checkpoint
    (m.DATA/'app/retention-last.json').write_text(json.dumps({'status':status,'dry_run':False,
        'errors':errors,'observability':{'backlog_status':backlog}}))
    m.seal();backup(2)
    with pytest.raises(ValueError, match='자료 정리 완료'):m.record()
    assert not m.PROOF.exists()


def test_failed_unseal_invalidates_proof_first_and_keeps_recovery_marker(checkpoint, monkeypatch):
    m, _, _ = completed(checkpoint)
    def failed(query):raise RuntimeError('connection lost')
    monkeypatch.setattr(m, 'sql', failed)
    with pytest.raises(RuntimeError):m.writable()
    assert not m.PROOF.exists() and m.SEAL.exists()


def test_dml_and_ddl_counters_include_privileged_writes_but_not_reads(checkpoint):
    m, _, _ = checkpoint
    for name in ('Com_insert', 'Com_insert_select', 'Com_update_multi', 'Com_delete_multi',
                 'Com_truncate', 'Com_drop_db', 'Com_alter_table', 'Com_create_trigger', 'Com_load'):
        assert m.WRITE.match(name)
    for name in ('Com_select', 'Com_show_status', 'Com_show_tables', 'Com_commit', 'Com_begin'):
        assert not m.WRITE.match(name)


@pytest.mark.parametrize('failure', ['', 'stop', 'pre_backup', 'cleanup', 'seal', 'final_backup', 'record'])
def test_nightly_stage_failures_never_publish_success_or_delete_without_backup(tmp_path, failure):
    import os
    import subprocess
    from test_gcp_deployment import executable
    root=tmp_path/'repo';deploy=root/'deploy/gcp';deploy.mkdir(parents=True)
    etc=tmp_path/'etc';etc.mkdir();(etc/'backup-status').write_text('UPLOADED previous')
    calls=tmp_path/'calls';bindir=tmp_path/'bin';bindir.mkdir()
    executable(bindir/'docker', '''echo "docker $*" >> "$CALLS"
if [[ "$*" == *"stop -t 180 app"* && "$FAILURE" == stop ]]; then exit 1; fi
if [[ "$*" == *"--services db"* ]]; then echo db; fi
''')
    executable(bindir/'systemctl', 'echo systemctl >> "$CALLS"\n')
    executable(bindir/'sync', 'echo sync >> "$CALLS"\n')
    executable(deploy/'inspect.sh', 'echo inspection >> "$CALLS"\n')
    executable(deploy/'backup.sh', '''count=$(cat "$COUNT" 2>/dev/null || echo 0)
count=$((count+1)); echo "$count" > "$COUNT"; echo "backup-$count" >> "$CALLS"
if [[ ($count == 1 && "$FAILURE" == pre_backup) || ($count == 2 && "$FAILURE" == final_backup) ]]; then
    echo 'FAILED test' > "$RECEIPT"; exit 1
fi
echo "UPLOADED backup-$count" > "$RECEIPT"
''')
    executable(deploy/'retention_after_update.sh', 'echo cleanup >> "$CALLS"\n[[ "$FAILURE" != cleanup ]]\n')
    (deploy/'nightly_checkpoint.py').write_text('''import os,sys
with open(os.environ['CALLS'],'a') as f:f.write(sys.argv[1]+'\\n')
if sys.argv[1]==os.environ['FAILURE']:raise SystemExit(1)
''')
    script=tmp_path/'stop.sh'
    script.write_text((DEPLOY/'stop.sh').read_text().replace('/opt/sessionquant',str(root))
        .replace('/etc/sessionquant',str(etc)).replace('/run/sessionquant-stop.lock',str(tmp_path/'stop.lock'))
        .replace('/run/sessionquant-update.lock',str(tmp_path/'update.lock')))
    result=subprocess.run(['bash',str(script)],capture_output=True,text=True,timeout=10,
        env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],'CALLS':str(calls),
             'FAILURE':failure,'COUNT':str(tmp_path/'count'),'RECEIPT':str(etc/'backup-status')})
    lines=calls.read_text().splitlines()
    assert (result.returncode==0) is (not failure)
    if failure=='stop':assert 'backup-1' not in lines and 'cleanup' not in lines
    elif failure=='pre_backup':assert 'cleanup' not in lines and 'seal' not in lines
    elif failure=='cleanup':assert 'seal' not in lines and 'backup-2' not in lines
    elif failure=='seal':assert 'backup-2' not in lines and 'record' not in lines
    elif failure=='final_backup':assert 'record' not in lines
    else:
        assert lines.index('backup-1') < lines.index('cleanup') < lines.index('seal') < lines.index('backup-2') < lines.index('record')
    assert ('야간 정리·최종 백업 완료.' in result.stdout) is (not failure)
    assert not any('down' in line for line in lines)
    if failure!='stop':assert lines[-2:]==['sync','inspection']


def test_host_health_report_does_not_invalidate_sealed_application_data(checkpoint):
    m, _, _ = completed(checkpoint)
    (m.DATA/'health').mkdir()
    (m.DATA/'health/storage.json').write_text('{"status":"OK"}')
    m.verify()
    (m.DATA/'health/storage.json').write_text('{"status":"LOW"}')
    m.verify()
