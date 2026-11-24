"""클라우드 자원을 만들지 않고 업로드·종료 스크립트의 경계를 검증한다."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess

import pytest

DEPLOY=Path(__file__).parents[1]/'deploy'/'gcp'


def uploader():
    spec=importlib.util.spec_from_file_location('upload_backup',DEPLOY/'upload_backup.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('status', [200,403])
def test_backup_upload_uses_precondition_streaming_and_private_metadata(tmp_path,monkeypatch,status):
    module=uploader();path=tmp_path/'backup.tar.gz';path.write_bytes(b'x'*(2*1024*1024+7))
    events=[]
    class Metadata(io.BytesIO):
        headers={'Metadata-Flavor':'Google'}
    def metadata(request,timeout):
        assert request.full_url.startswith('http://metadata.google.internal/')
        assert request.get_header('Metadata-flavor')=='Google'
        return Metadata(b'{"access_token":"test-token"}')
    class Connection:
        def __init__(self,host,timeout):assert host=='storage.googleapis.com'
        def putrequest(self,method,url):
            assert method=='POST' and 'ifGenerationMatch=0' in url and 'daily%2Fbackup.tar.gz' in url
        def putheader(self,name,value):events.append((name,value))
        def endheaders(self):pass
        def send(self,chunk):events.append(('chunk',len(chunk)))
        def getresponse(self):
            class Response:
                def read(self,limit):return json.dumps({'size':str(path.stat().st_size)}).encode()
            response=Response();response.status=status;return response
        def close(self):events.append(('closed',True))
    monkeypatch.setattr(module,'urlopen',metadata)
    monkeypatch.setattr(module.http.client,'HTTPSConnection',Connection)
    if status==200:module.upload(path,'private-backups')
    else:
        with pytest.raises(RuntimeError,match='HTTP 403'):module.upload(path,'private-backups')
    assert ('Authorization','Bearer test-token') in events
    assert [value for key,value in events if key=='chunk']==[1024*1024,1024*1024,7]
    assert events[-1]==('closed',True)


def executable(path,body):
    path.write_text('#!/bin/bash\n'+body)
    path.chmod(0o755)


@pytest.mark.skipif(os.name=='nt',reason='GCE Linux shell scripts')
@pytest.mark.parametrize('failed_backup',[False,True])
def test_close_saves_backs_up_and_opens_inspection_without_poweroff(tmp_path,failed_backup):
    root=tmp_path/'repo';(root/'deploy/gcp').mkdir(parents=True)
    disk=root/'database-disk';disk.write_text('preserved')
    calls=tmp_path/'calls'
    bindir=tmp_path/'bin';bindir.mkdir()
    executable(bindir/'docker','echo "docker $*" >> "$CALLS"\nif [[ "$*" == *"--services db"* ]]; then echo db; fi\n')
    executable(bindir/'systemctl','echo "systemctl $*" >> "$CALLS"\n')
    executable(bindir/'sync','echo sync >> "$CALLS"\n')
    executable(root/'deploy/gcp/backup.sh',f'echo backup >> "$CALLS"\nexit {int(failed_backup)}\n')
    executable(root/'deploy/gcp/retention_after_update.sh','echo retention >> "$CALLS"\n')
    executable(root/'deploy/gcp/inspect.sh','echo inspection >> "$CALLS"\n')
    (root/'deploy/gcp'/'nightly_checkpoint.py').write_text('pass\n')
    script=tmp_path/'stop.sh'
    # The disposable copy cannot execute host poweroff or access production paths.
    body=(DEPLOY/'stop.sh').read_text().replace('/opt/sessionquant',str(root)).replace('/run/sessionquant-update.lock',str(tmp_path/'update.lock')).replace(
        '/run/sessionquant-stop.lock',str(tmp_path/'stop.lock')).replace('/usr/bin/systemctl',str(bindir/'systemctl')).replace(
        '/etc/sessionquant',str(tmp_path/'etc'))
    script.write_text(body)
    result=subprocess.run(['bash',str(script)],env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],
        'CALLS':str(calls)},capture_output=True,text=True,timeout=10)
    assert result.returncode==int(failed_backup) and disk.read_text()=='preserved'
    lines=calls.read_text().splitlines()
    assert 'stop -t 180 app' in lines[1]
    assert 'systemctl stop sessionquant-market-close.timer' in lines[0]
    assert lines.index('backup') < lines.index('inspection')
    assert not any('down --timeout' in line for line in lines)
    assert ('retention' in lines) is (not failed_backup)
    if not failed_backup: assert lines.index('backup') < lines.index('retention')
    assert lines[-2:]==['sync','inspection']
    assert not any('poweroff' in line for line in lines)
    assert not any(' -v' in line for line in lines)
    assert ('백업 실패' in result.stderr) is failed_backup
    if failed_backup: assert (tmp_path/'etc/backup-status').read_text().startswith('FAILED ')


@pytest.mark.skipif(os.name=='nt',reason='GCE Linux shell scripts')
@pytest.mark.parametrize('delay,error,started',[(7200,False,True),(0,False,False),(0,True,False)])
def test_start_arms_shutdown_before_starting_app(tmp_path,delay,error,started):
    root=tmp_path/'repo';root.mkdir()
    (root/'deploy/gcp').mkdir(parents=True)
    executable(root/'deploy/gcp/inspect.sh','echo inspection >> "$CALLS"\n')
    (root/'deploy/gcp'/'nightly_checkpoint.py').write_text('pass\n')
    calls=tmp_path/'calls';bindir=tmp_path/'bin';bindir.mkdir()
    executable(bindir/'docker',f'echo "docker $*" >> "$CALLS"\nif [[ "$1" == run ]]; then echo {delay}; exit {int(error)}; fi\n')
    executable(bindir/'systemctl','echo "systemctl $*" >> "$CALLS"\nif [[ "$1" == is-active ]]; then exit 3; fi\n')
    executable(bindir/'systemd-run','echo "timer $*" >> "$CALLS"\n')
    # Use a known monotonic epoch plus the requested delay; storage is separate.
    executable(bindir/'python3','if [[ "$1" == - ]]; then echo "$((1000000000 + $2 * 1000000))"; fi\n')
    script=tmp_path/'start.sh'
    script.write_text((DEPLOY/'start.sh').read_text().replace('/opt/sessionquant',str(root)))
    result=subprocess.run(['bash',str(script)],env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],
        'CALLS':str(calls)},capture_output=True,text=True,timeout=10)
    lines=calls.read_text().splitlines()
    timers=[i for i,line in enumerate(lines) if line.startswith('timer ')]
    apps=[i for i,line in enumerate(lines) if ' up -d --wait' in line]
    assert len(timers)==(2 if started else 0) and bool(apps) is started
    assert ('inspection' in lines) is (not started)
    if started:
        assert timers[-1]<apps[0]
        for index,seconds in zip(timers,(delay,72000)):
            assert f'--on-boot={1_000_000_000+seconds*1_000_000}us' in lines[index]
            assert '--on-active' not in lines[index]
    assert not any('poweroff' in line for line in lines)
    assert result.returncode==int(error)


@pytest.mark.skipif(os.name=='nt',reason='GCE Linux shell scripts')
@pytest.mark.parametrize('compressed', [False, True])
@pytest.mark.parametrize('tables,running,allowed',[(0,False,True),(12,False,False),(0,True,False)])
def test_import_only_accepts_empty_database_with_app_stopped(tmp_path,tables,running,allowed,compressed):
    import gzip
    root=tmp_path/'repo';root.mkdir();bindir=tmp_path/'bin';bindir.mkdir()
    payload=b'CREATE TABLE fixture (id INT);'
    dump=tmp_path/('source.sql.gz' if compressed else 'source.sql')
    dump.write_bytes(gzip.compress(payload) if compressed else payload)
    copied=tmp_path/'copied.sql'
    executable(bindir/'docker',f'''if [[ "$*" == *"--services app"* ]]; then
  {'echo app' if running else ':'}
elif [[ "$*" == *"information_schema.tables"* ]]; then
  echo {tables}
else
  cat > "$COPIED"
fi
''')
    script=tmp_path/'import.sh'
    script.write_text((DEPLOY/'import_database.sh').read_text().replace('/opt/sessionquant',str(root)))
    result=subprocess.run(['bash',str(script),str(dump)],env={**os.environ,
        'PATH':str(bindir)+':'+os.environ['PATH'],'COPIED':str(copied)},capture_output=True,text=True,timeout=10)
    assert (result.returncode==0) is allowed and copied.exists() is allowed
    if allowed:assert copied.read_bytes()==payload


def test_storage_thresholds_and_inode_exhaustion_fail_closed(tmp_path, monkeypatch):
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('storage_check', DEPLOY/'storage_check.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stats = dict(f_bavail=2048, f_frsize=1024*1024, f_files=1000, f_favail=900)
    monkeypatch.setattr(module.os, 'statvfs', lambda path: SimpleNamespace(**stats))
    assert module.check([tmp_path], 2048)
    stats['f_bavail'] = 2047
    assert not module.check([tmp_path], 2048)
    stats.update(f_bavail=4096, f_favail=49)
    assert not module.check([tmp_path], 2048)
    def unavailable(path):
        raise OSError('fixture')
    monkeypatch.setattr(module.os, 'statvfs', unavailable)
    assert not module.check([tmp_path], 2048)


@pytest.mark.skipif(os.name=='nt',reason='GCE Linux shell scripts')
@pytest.mark.parametrize('failure', ['', 'dump', 'upload', 'space'])
def test_backup_streams_compressed_sql_and_preserves_previous_backup_on_failure(tmp_path, failure):
    import tarfile
    import gzip
    import shutil
    root=tmp_path/'repo'; deploy=root/'deploy/gcp'; deploy.mkdir(parents=True)
    data=tmp_path/'data'; (data/'app').mkdir(parents=True)
    (data/'app/state.json').write_text('{"fixture":true}')
    backups=data/'backups'; backups.mkdir()
    old=backups/'sessionquant-20000101.tar.gz'; old.write_bytes(b'previous backup')
    etc=tmp_path/'etc'; etc.mkdir()
    bindir=tmp_path/'bin'; bindir.mkdir()
    executable(bindir/'docker', '''if [[ "$*" == *"--services app"* ]]; then exit 0; fi
printf 'CREATE TABLE fixture (id INT);\\n'
if [[ "$FAILURE" == dump ]]; then exit 1; fi
''')
    (deploy/'storage_check.py').write_text('import os; raise SystemExit(int(os.environ["FAILURE"]=="space"))')
    (deploy/'upload_backup.py').write_text('import os; raise SystemExit(int(os.environ["FAILURE"]=="upload"))')
    shutil.copyfile(DEPLOY/'package_backup.py',deploy/'package_backup.py')
    body=(DEPLOY/'backup.sh').read_text().replace('/opt/sessionquant',str(root)).replace(
        '/var/lib/sessionquant',str(data)).replace('/etc/sessionquant',str(etc)).replace(
        '/run/sessionquant-backup.lock',str(tmp_path/'backup.lock'))
    script=tmp_path/'backup.sh'; script.write_text(body)
    result=subprocess.run(['bash',str(script)],env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],
        'FAILURE':failure},capture_output=True,text=True,timeout=20)
    assert (result.returncode==0) == (not failure), result.stderr
    assert old.read_bytes()==b'previous backup'
    assert not list(backups.glob('.working.*'))
    completed=[p for p in backups.glob('sessionquant-*.tar.gz') if p!=old]
    assert len(completed)==(1 if failure in ('', 'upload') else 0)
    assert (etc/'backup-status').read_text().startswith('FAILED' if failure else 'UPLOADED')
    if failure:
        phase={'dump':'DUMP','upload':'UPLOAD','space':'PREPARE'}[failure]
        assert f'phase={phase}' in (etc/'backup-status').read_text()
        assert 'elapsed_seconds=' in (etc/'backup-status').read_text()
    else:
        for phase in ('DUMP','VERIFY_SQL','PACKAGE','VERIFY_ARCHIVE','UPLOAD'):
            assert f'백업 단계 완료: {phase}' in result.stdout
    if completed:
        with tarfile.open(completed[0]) as archive:
            assert 'database.sql' not in archive.getnames()
            assert gzip.decompress(archive.extractfile('database.sql.gz').read()).startswith(b'CREATE TABLE')
            assert archive.extractfile('app/state.json').read()==b'{"fixture":true}'


@pytest.mark.skipif(os.name=='nt',reason='GCE Linux shell scripts')
@pytest.mark.parametrize('healthy,maintenance', [(True,False), (False,False), (False,True)])
def test_storage_guard_stops_only_app_and_latches_without_deleting_data(tmp_path, healthy, maintenance):
    root=tmp_path/'repo'; root.mkdir(); etc=tmp_path/'etc'; etc.mkdir()
    if maintenance: (etc/'maintenance').touch()
    calls=tmp_path/'calls'; bindir=tmp_path/'bin'; bindir.mkdir()
    executable(bindir/'docker','echo "$*" >> "$CALLS"\nif [[ "$*" == *"--services app"* ]]; then echo app; fi\n')
    executable(bindir/'python3',f'exit {0 if healthy else 1}\n')
    script=tmp_path/'guard.sh'; script.write_text((DEPLOY/'storage_guard.sh').read_text().replace(
        '/opt/sessionquant',str(root)).replace('/etc/sessionquant',str(etc)))
    result=subprocess.run(['bash',str(script)],env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],
        'CALLS':str(calls)},capture_output=True,text=True,timeout=10)
    stopped=not healthy and not maintenance
    assert result.returncode==int(stopped)
    assert (etc/'storage-blocked').exists()==stopped
    log=calls.read_text() if calls.exists() else ''
    assert ('stop -t 180 app' in log)==stopped
    assert 'down' not in log and 'stop -t 180 db' not in log


@pytest.mark.skipif(os.name=='nt',reason='GCE Linux shell scripts')
@pytest.mark.parametrize('condition', ['maintenance', 'storage-blocked', 'backup-failed', 'low-space'])
def test_start_never_starts_app_when_maintenance_or_storage_recovery_is_needed(tmp_path,condition):
    root=tmp_path/'repo'; root.mkdir(); etc=tmp_path/'etc'; etc.mkdir()
    if condition in ('maintenance', 'storage-blocked'): (etc/condition).touch()
    if condition=='backup-failed': (etc/'backup-status').write_text('FAILED fixture\n')
    calls=tmp_path/'calls'; bindir=tmp_path/'bin'; bindir.mkdir()
    executable(bindir/'docker','echo "docker $*" >> "$CALLS"\nif [[ "$1" == run ]]; then echo 3600; fi\n')
    executable(bindir/'systemctl','if [[ "$1" == is-active ]]; then exit 3; fi\n')
    executable(bindir/'systemd-run','echo timer >> "$CALLS"\n')
    executable(bindir/'python3',f'if [[ "$1" == - ]]; then echo 3600000000; else exit {int(condition=="low-space")}; fi\n')
    script=tmp_path/'start.sh'; script.write_text((DEPLOY/'start.sh').read_text().replace(
        '/opt/sessionquant',str(root)).replace('/etc/sessionquant',str(etc)))
    result=subprocess.run(['bash',str(script)],env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],
        'CALLS':str(calls)},capture_output=True,text=True,timeout=10)
    assert result.returncode == (0 if condition=='maintenance' else 1)
    log=calls.read_text() if calls.exists() else ''
    assert 'up -d' not in log
    assert ('timer' in log) == (condition!='maintenance')


@pytest.mark.skipif(os.name=='nt',reason='GCE Linux shell scripts')
@pytest.mark.parametrize('operating_status', ['TERMINATED', 'RUNNING'])
def test_cloudshell_preparation_snapshots_before_disk_changes_and_never_starts_vm(tmp_path,operating_status):
    bindir=tmp_path/'bin'; bindir.mkdir(); calls=tmp_path/'calls'
    executable(bindir/'gcloud', '''echo "$*" >> "$CALLS"
case "$*" in
  *"snapshots describe"*) echo READY ;;
  *"describe sessionquant-paper"*"value(status)"*) echo "$OPERATING_STATUS" ;;
  *"describe sessionquant-recovery"*"value(status)"*) echo RUNNING ;;
  *"describe sessionquant-paper"*"json(disks)"*) echo '{"disks":[{"boot":true,"source":"disks/sessionquant-ubuntu-boot"}]}' ;;
  *"describe sessionquant-recovery"*"disks.deviceName"*) echo 'persistent-disk-0;sq-recovery;sq-postclose' ;;
esac
''')
    result=subprocess.run(['bash',str(DEPLOY/'cloudshell_prepare_storage.sh')],env={**os.environ,
        'PATH':str(bindir)+':'+os.environ['PATH'],'CALLS':str(calls),'OPERATING_STATUS':operating_status},
        capture_output=True,text=True,timeout=10)
    log=calls.read_text()
    assert 'instances start' not in log and ' delete ' not in log
    if operating_status=='RUNNING':
        assert result.returncode!=0 and 'detach-disk' not in log
    else:
        assert result.returncode==0, result.stderr
        assert log.index('snapshots describe') < log.index('detach-disk')
        assert log.index('detach-disk sessionquant-paper') < log.index('device-name=sq-operating')
        assert log.index('sq-offline-maintenance.sh" /mnt/sq-operating') < log.index('attach-disk sessionquant-paper')
        assert '--boot --device-name=sessionquant-ubuntu-boot' in log
        assert 'set-disk-auto-delete sessionquant-paper' in log and '--no-auto-delete' in log


@pytest.mark.skipif(os.name=='nt' or not hasattr(os,'geteuid') or os.geteuid()!=0, reason='Linux offline root fixture')
@pytest.mark.parametrize('writable', [True, False])
def test_offline_maintenance_masks_units_and_preserves_old_unit_without_touching_db(tmp_path,writable):
    root=tmp_path/'disk'; (root/'etc/sessionquant').mkdir(parents=True)
    units=root/'etc/systemd/system'; units.mkdir(parents=True)
    unit=units/'sessionquant.service'; unit.write_text('original unit')
    deploy=root/'opt/sessionquant/deploy/gcp'; deploy.mkdir(parents=True); (deploy/'compose.yaml').touch()
    data=root/'var/lib/sessionquant/mysql'; data.mkdir(parents=True); (data/'fixture.ibd').write_bytes(b'preserved')
    bindir=tmp_path/'bin'; bindir.mkdir()
    executable(bindir/'mountpoint', 'exit 0\n')
    executable(bindir/'sync', 'exit 0\n')
    executable(bindir/'findmnt', '''if [[ "$*" == *OPTIONS* ]]; then echo "$OPTIONS";
elif [[ "$*" == *--mountpoint* ]]; then echo /dev/sdz1; else echo /dev/sda1; fi
''')
    executable(bindir/'readlink', '''if [[ "$*" == *google-sq-operating-part1* ]]; then echo /dev/sdz1;
else exec /usr/bin/readlink "$@"; fi
''')
    result=subprocess.run(['bash',str(DEPLOY/'offline_maintenance.sh'),str(root)],env={**os.environ,
        'PATH':str(bindir)+':'+os.environ['PATH'],'OPTIONS':'rw,relatime' if writable else 'ro,relatime'},
        capture_output=True,text=True,timeout=10)
    assert (result.returncode==0)==writable, result.stderr
    assert (data/'fixture.ibd').read_bytes()==b'preserved'
    assert (root/'etc/sessionquant/maintenance').exists()==writable
    if writable:
        assert unit.is_symlink() and os.readlink(unit)=='/dev/null'
        assert (root/'etc/sessionquant/units-before-storage-repair/sessionquant.service').read_text()=='original unit'
    else:
        assert unit.read_text()=='original unit'


def test_start_deadlines_cover_database_and_application_health_startup():
    import re
    import yaml
    compose=yaml.safe_load((DEPLOY/'compose.yaml').read_text())
    app=compose['services']['app']['healthcheck'];db=compose['services']['db']['healthcheck']
    seconds=lambda value:int(str(value).removesuffix('s'))
    probe_budget=lambda cfg:(seconds(cfg['interval'])+seconds(cfg['timeout']))*cfg['retries']
    wait=int(re.findall(r'up -d --wait --wait-timeout (\d+)',(DEPLOY/'start.sh').read_text())[-1])
    outer=int(re.search(r'TimeoutStartSec=(\d+)',(DEPLOY/'sessionquant.service').read_text())[1])
    assert seconds(app['start_period'])+probe_budget(app)<wait<outer
    assert seconds(db['interval'])*db['retries']+seconds(app['start_period'])+probe_budget(app)<wait
    assert '/_stcore/health' in app['test'][-1] and app['test'][0]=='CMD'


@pytest.mark.skipif(os.getenv('SQ_TEST_GCP_STARTUP')!='1',reason='requires disposable local Docker health fixture')
@pytest.mark.parametrize('grace',[False,True])
def test_docker_grace_allows_slow_listener_without_hiding_probe_failures(tmp_path,grace):
    import uuid
    import yaml
    name='sq-startup-'+uuid.uuid4().hex[:12]
    health=yaml.safe_load((DEPLOY/'compose.yaml').read_text())['services']['app']['healthcheck']
    # Same HTTP probe, accelerated time scale. No host ports, mounts, or network.
    health.update(start_period='8s' if grace else '0s',interval='1s',timeout='1s',retries=2)
    server='''import time
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/_stcore/health' else 404)
        self.end_headers()
time.sleep(4)
HTTPServer(('127.0.0.1',8501),Handler).serve_forever()
'''
    compose=tmp_path/'compose.yaml'
    compose.write_text(yaml.safe_dump({'name':name,'services':{'app':{
        'image':os.getenv('SQ_STARTUP_TEST_IMAGE','sessionquant:gcp-review'),
        'network_mode':'none','command':['python','-c',server],'healthcheck':health}}}))
    args=['docker','compose','-f',str(compose)]
    try:
        result=subprocess.run([*args,'up','-d','--wait','--wait-timeout','20'],capture_output=True,text=True,timeout=40)
        assert (result.returncode==0)==grace,result.stderr
        if not grace:assert 'unhealthy' in result.stderr
    finally:
        subprocess.run([*args,'down','--timeout','2'],capture_output=True,timeout=20,check=True)


@pytest.mark.parametrize('profile',[None,'config/sessionquant.strategy-lab-websocket.example.yaml'])
def test_container_profile_is_one_argument_and_default_is_unchanged(tmp_path,profile):
    import json
    import yaml
    command=yaml.safe_load((DEPLOY/'compose.yaml').read_text())['services']['app']['command']
    # Compose emits a single dollar for $$. Exercise the resulting POSIX shell
    # command with a disposable Python stub, without Docker or production env.
    stub=tmp_path/'python'
    stub.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    stub.chmod(0o755)
    env={**os.environ,'PATH':str(tmp_path)+os.pathsep+os.environ['PATH']}
    env.pop('SQ_SESSION_CONFIG',None)
    if profile:env['SQ_SESSION_CONFIG']=profile
    result=subprocess.run([part.replace('$$','$') for part in command],env=env,capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)==['-m','sessionquant','run','--config',
        profile or 'config/sessionquant.realtime-paper.example.yaml','--no-desktop-notifications']
