"""Measured production failure, live reserve and status are independent of SQL."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('backup_capacity', ROOT/'deploy/gcp/backup_capacity.py')
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


def test_production_dump_needs_two_copies_and_bootstrap_detects_shortfall():
    gib = capacity.GIB
    predicted, basis = capacity.estimate(46*gib, 4*gib, {
        'compressed_bytes': 7924747948, 'database_bytes': 46*gib})
    assert basis == 'MEASURED_DUMP_WITH_GROWTH'
    assert predicted >= 7924747948
    assert predicted*2+capacity.RESERVE > 11*gib
    bootstrap, _ = capacity.estimate(46*gib, 4*gib, {})
    assert 11*gib < bootstrap*2+capacity.RESERVE < 50*gib


def test_estimate_grows_but_does_not_shrink_when_db_allocation_drops():
    base = {'compressed_bytes': 8*capacity.GIB, 'database_bytes': 40*capacity.GIB}
    assert capacity.estimate(80*capacity.GIB, 0, base)[0] == 20*capacity.GIB
    assert capacity.estimate(20*capacity.GIB, 0, base)[0] == 10*capacity.GIB
    assert capacity.estimate(80, 0, {'compressed_bytes': True, 'database_bytes': -1})[1] == 'BOOTSTRAP_ESTIMATE'


def test_dump_reserves_second_copy_before_writing_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, 'CHUNK', 4)
    available = [capacity.RESERVE + 20]
    monkeypatch.setattr(capacity, 'free_bytes', lambda path: available[0])
    output = tmp_path/'dump.gz.part'
    class Source(io.BytesIO):
        def read(self, size):
            if self.tell():
                available[0] = capacity.RESERVE + 11  # next write needs 4+2*4=12
            return super().read(size)
    with pytest.raises(OSError, match='백업 공간 부족'):
        capacity.write_dump(Source(b'abcdefgh'), output, 0)
    assert output.read_bytes() == b'abcd'
    with pytest.raises(FileExistsError):
        capacity.write_dump(io.BytesIO(b'overwrite'), output, 0)
    assert output.read_bytes() == b'abcd'


def test_sufficient_space_preserves_compressed_stream_exactly(tmp_path, monkeypatch):
    import gzip
    raw = gzip.compress(b'CREATE TABLE fixture(id INT);')
    monkeypatch.setattr(capacity, 'free_bytes', lambda path: 10*capacity.GIB)
    target = tmp_path/'dump.gz'
    assert capacity.write_dump(io.BytesIO(raw), target, 0) == len(raw)
    assert target.read_bytes() == raw


def test_monitor_metadata_only_separate_from_sealed_app_and_one_report(tmp_path):
    data, etc = tmp_path/'data', tmp_path/'etc'
    for name in ('mysql', 'app', 'backups'):
        (data/name).mkdir(parents=True)
    etc.mkdir()
    (data/'mysql/table.ibd').write_bytes(b'database')
    (data/'mysql/external').symlink_to('/dev/zero')
    (data/'app/state.json').write_text('preserved')
    (etc/'backup-status').write_text('FAILED now phase=PACKAGE')
    (etc/'retention-status').write_text('SKIPPED now reason=BACKUP_FAILED')
    for _ in range(3):
        report = capacity.measure(data, etc)
        capacity.publish(data, report)
    assert report['database_file_bytes'] == 8
    assert report['largest_database_files'] == [{'file':'table.ibd','bytes':8}]
    assert report['backup_status'].startswith('FAILED')
    assert report['retention_status'].startswith('SKIPPED')
    assert list((data/'health').iterdir()) == [data/'health/storage.json']
    assert (data/'health/storage.json').stat().st_size < 16384
    assert (data/'health/storage.json').stat().st_mode & 0o777 == 0o644
    assert list((data/'app').iterdir()) == [data/'app/state.json']
    assert (data/'app/state.json').read_text() == 'preserved'
    assert json.loads((data/'health/storage.json').read_text()) == report


def test_monitor_is_outside_nightly_file_identity():
    # See test_nightly_checkpoint.py for the actual reuse validation.
    assert '/var/lib/sessionquant/health:/app/health:ro' in (ROOT/'deploy/gcp/compose.yaml').read_text()


def test_low_capacity_check_aborts_before_dump_and_preserves_previous(tmp_path):
    import os
    import subprocess
    from test_gcp_deployment import executable, DEPLOY
    root, data, etc = tmp_path/'repo', tmp_path/'data', tmp_path/'etc'
    deploy = root/'deploy/gcp'
    deploy.mkdir(parents=True)
    for name in ('mysql', 'app', 'backups'):
        (data/name).mkdir(parents=True)
    etc.mkdir()
    (data/'backups/sessionquant-previous.tar.gz').write_bytes(b'keep')
    (etc/'backup-capacity.json').write_text(json.dumps({'compressed_bytes':10**18,'database_bytes':1}))
    bindir = tmp_path/'bin'; bindir.mkdir()
    calls = tmp_path/'calls'
    executable(bindir/'docker', 'echo "$*" >> "$CALLS"\nif [[ "$*" == *"--services app"* ]]; then exit 0; fi\nexit 42\n')
    (deploy/'storage_check.py').write_text('pass')
    for name in ('backup_capacity.py','backup.sh'):
        (deploy/name).write_text((DEPLOY/name).read_text().replace('/opt/sessionquant',str(root))
            .replace('/var/lib/sessionquant',str(data)).replace('/etc/sessionquant',str(etc))
            .replace('/run/sessionquant-backup.lock',str(tmp_path/'backup.lock')))
    result = subprocess.run(['bash',str(deploy/'backup.sh')], env={**os.environ,
        'PATH':str(bindir)+':'+os.environ['PATH'], 'CALLS':str(calls)}, capture_output=True,text=True,timeout=10)
    assert result.returncode != 0
    assert len(calls.read_text().splitlines()) == 1  # app status only, no dump
    assert 'phase=CAPACITY' in (etc/'backup-status').read_text()
    assert (data/'backups/sessionquant-previous.tar.gz').read_bytes() == b'keep'
    assert not list((data/'backups').glob('.working.*'))
