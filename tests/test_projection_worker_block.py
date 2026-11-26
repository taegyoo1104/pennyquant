"""Uncertain projection worker shutdown must block lifecycle and standalone backup."""
import os
from pathlib import Path
import subprocess

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy/gcp'


@pytest.mark.skipif(os.name == 'nt', reason='GCE Linux shell scripts')
@pytest.mark.parametrize('script', ['lifecycle_lock.sh', 'backup.sh'])
def test_uncertain_worker_blocks_before_runtime_or_backup_commands(tmp_path, script):
    etc = tmp_path / 'etc'
    etc.mkdir()
    (etc / 'projection-preparation-blocked').write_text('sessionquant-projections-fixture\n')
    calls = tmp_path / 'calls'
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    docker = bindir / 'docker'
    docker.write_text('#!/bin/bash\necho unexpected >> "$SQ_TEST_CALLS"\nexit 1\n')
    docker.chmod(0o755)
    body = (DEPLOY / script).read_text().replace('/opt/sessionquant', str(tmp_path))
    body = body.replace('/etc/sessionquant', str(etc)).replace('/run/sessionquant-', str(tmp_path / 'lock-'))
    runner = tmp_path / 'runner.sh'
    runner.write_text(body + '\necho continued >> "$SQ_TEST_CALLS"\n')
    result = subprocess.run(['bash', str(runner)], capture_output=True, text=True, timeout=5,
        env={**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH'], 'SQ_TEST_CALLS': str(calls)})
    assert result.returncode == 1
    assert not calls.exists()
    assert '조회 요약 준비 작업' in result.stderr
    if script == 'backup.sh':
        assert 'phase=PROJECTION_WORKER' in (etc / 'backup-status').read_text()
