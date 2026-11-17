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
def test_shutdown_orders_save_backup_stop_and_poweroff_without_deleting_disk(tmp_path,failed_backup):
    root=tmp_path/'repo';(root/'deploy/gcp').mkdir(parents=True)
    disk=root/'database-disk';disk.write_text('preserved')
    calls=tmp_path/'calls'
    bindir=tmp_path/'bin';bindir.mkdir()
    executable(bindir/'docker','echo "docker $*" >> "$CALLS"\nif [[ "$*" == *"--services db"* ]]; then echo db; fi\n')
    executable(bindir/'systemctl','echo "systemctl $*" >> "$CALLS"\n')
    executable(bindir/'sync','echo sync >> "$CALLS"\n')
    executable(root/'deploy/gcp/backup.sh',f'echo backup >> "$CALLS"\nexit {int(failed_backup)}\n')
    script=tmp_path/'stop.sh'
    # The disposable copy cannot execute host poweroff or access production paths.
    body=(DEPLOY/'stop.sh').read_text().replace('/opt/sessionquant',str(root)).replace(
        '/run/sessionquant-stop.lock',str(tmp_path/'stop.lock')).replace('/usr/bin/systemctl',str(bindir/'systemctl'))
    script.write_text(body)
    result=subprocess.run(['bash',str(script)],env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],
        'CALLS':str(calls)},capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and disk.read_text()=='preserved'
    lines=calls.read_text().splitlines()
    assert 'stop -t 180 app' in lines[0]
    assert lines.index('backup')<next(i for i,line in enumerate(lines) if 'down --timeout 120' in line)
    assert lines[-2:]==['sync','systemctl poweroff']
    assert not any(' -v' in line for line in lines)
    assert ('백업 실패' in result.stderr) is failed_backup


@pytest.mark.skipif(os.name=='nt',reason='GCE Linux shell scripts')
@pytest.mark.parametrize('delay,error,started',[(7200,False,True),(0,False,False),(0,True,False)])
def test_start_arms_shutdown_before_starting_app(tmp_path,delay,error,started):
    root=tmp_path/'repo';root.mkdir()
    calls=tmp_path/'calls';bindir=tmp_path/'bin';bindir.mkdir()
    executable(bindir/'docker',f'echo "docker $*" >> "$CALLS"\nif [[ "$1" == run ]]; then echo {delay}; exit {int(error)}; fi\n')
    executable(bindir/'systemctl','echo "systemctl $*" >> "$CALLS"\nif [[ "$1" == is-active ]]; then exit 3; fi\n')
    executable(bindir/'systemd-run','echo "timer $*" >> "$CALLS"\n')
    script=tmp_path/'start.sh'
    script.write_text((DEPLOY/'start.sh').read_text().replace('/opt/sessionquant',str(root)))
    result=subprocess.run(['bash',str(script)],env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],
        'CALLS':str(calls)},capture_output=True,text=True,timeout=10)
    lines=calls.read_text().splitlines()
    timers=[i for i,line in enumerate(lines) if line.startswith('timer ')]
    apps=[i for i,line in enumerate(lines) if ' up -d --wait' in line]
    assert len(timers)==1 and bool(apps) is started
    if started:assert timers[0]<apps[0]
    assert ('--on-active=900s' if error else f'--on-active={delay or 60}s') in lines[timers[0]]
    assert result.returncode==int(error)


@pytest.mark.skipif(os.name=='nt',reason='GCE Linux shell scripts')
@pytest.mark.parametrize('tables,running,allowed',[(0,False,True),(12,False,False),(0,True,False)])
def test_import_only_accepts_empty_database_with_app_stopped(tmp_path,tables,running,allowed):
    root=tmp_path/'repo';root.mkdir();bindir=tmp_path/'bin';bindir.mkdir()
    dump=tmp_path/'source.sql';dump.write_text('CREATE TABLE fixture (id INT);')
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
    if allowed:assert copied.read_bytes()==dump.read_bytes()
