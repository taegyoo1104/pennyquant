import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

DEPLOY = Path(__file__).parents[1] / 'deploy/gcp'


@pytest.mark.parametrize('mode', ['ok', 'invalid', 'trading', 'stuck'])
def test_projection_wrapper_reports_failures_and_blocks_uncertain_workers(tmp_path, mode):
    root = tmp_path / 'opt/sessionquant'
    scripts = root / 'deploy/gcp'
    scripts.mkdir(parents=True)
    health = tmp_path / 'health'
    health.mkdir()
    etc = tmp_path / 'etc'
    etc.mkdir()
    run = tmp_path / 'run'
    run.mkdir()
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    log = tmp_path / 'calls'
    worker = tmp_path / 'worker'
    replacements = {'/opt/sessionquant': str(root), '/var/lib/sessionquant/health': str(health),
                    '/etc/sessionquant': str(etc), '/run/sessionquant': str(run / 'sessionquant')}
    for name in ('prepare_db_projections.sh', 'lifecycle_lock.sh'):
        content = (DEPLOY / name).read_text()
        for old, new in replacements.items():
            content = content.replace(old, new)
        # This isolated command-boundary test runs on non-root CI workers.
        content = content.replace('$EUID == 0 && ', '')
        (scripts / name).write_text(content)
    (binaries / 'hostname').write_text('#!/bin/sh\necho sessionquant-paper\n')
    (binaries / 'hostname').chmod(0o755)
    (binaries / 'docker').write_text(f'#!{sys.executable}\n' + '''import json,os,pathlib,sys
a=sys.argv[1:]; mode=os.environ['MODE']; log=pathlib.Path(os.environ['LOG']); worker=pathlib.Path(os.environ['WORKER'])
with log.open('a') as f: f.write(json.dumps(a)+'\\n')
if a[0]=='compose' and 'run' in a:
    if mode=='stuck':
        worker.write_text(a[a.index('--name')+1]); sys.exit(124)
    print('broken output' if mode=='invalid' else '{"status":"COMPLETE","reason":"PROJECTIONS_READY"}')
elif a[0]=='compose' and 'ps' in a: print('app')
elif a[0]=='inspect':
    print('["sh","-c","engine"]' if mode=='trading' else '["python","-m","sessionquant.inspection_dashboard"]')
elif a[0]=='ps':
    if worker.exists(): print(worker.read_text())
elif a[0]=='rm':
    if mode=='stuck': sys.exit(1)
    worker.unlink(missing_ok=True)
else: raise SystemExit(2)
''')
    (binaries / 'docker').chmod(0o755)
    report = health / 'db-projections-last.json'
    report.write_text('{"status":"COMPLETE","reason":"OLD_RESULT"}')
    process = subprocess.run(['bash', str(scripts / 'prepare_db_projections.sh'), '--apply'],
        env={**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
             'MODE': mode, 'LOG': str(log), 'WORKER': str(worker)}, capture_output=True, text=True, timeout=10)
    commands = [json.loads(row) for row in log.read_text().splitlines()]
    if mode == 'ok':
        assert process.returncode == 0, process.stderr
        assert json.loads(report.read_text())['reason'] == 'PROJECTIONS_READY'
    else:
        assert process.returncode != 0
        if mode == 'invalid':
            assert json.loads(report.read_text())['reason'] == 'INVALID_WORKER_RESULT'
        if mode == 'trading':
            assert not any('run' in command for command in commands)
        if mode == 'stuck':
            assert json.loads(report.read_text())['reason'] == 'WORKER_STOP_UNCONFIRMED'
            assert (etc / 'projection-preparation-blocked').read_text().strip() == worker.read_text()
