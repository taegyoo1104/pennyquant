"""Large-backup deadline ordering, recoverable failures, and archive compatibility."""

import gzip
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import tarfile

import pytest

from test_gcp_deployment import DEPLOY, executable


def packager():
    spec = importlib.util.spec_from_file_location("package_backup", DEPLOY / "package_backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_keeps_gzip_tar_and_sql_layout_and_refuses_overwrite(tmp_path):
    raw = b"INSERT INTO fixture VALUES (" + os.urandom(256 * 1024).hex().encode() + b");\n"
    database = tmp_path / "database.sql.gz"
    database.write_bytes(gzip.compress(raw, compresslevel=1))
    app = tmp_path / "app"
    app.mkdir()
    (app / "state.json").write_text('{"fixture":true}')
    destination = tmp_path / "backup.tar.gz"
    packager().package(database, app, destination)
    # Read to EOF to check the gzip CRC, as the shell verifier does.
    with gzip.open(destination, "rb") as stream:
        assert stream.read()
    with tarfile.open(destination, "r:gz") as archive:
        assert set(archive.getnames()) == {"database.sql.gz", "app", "app/state.json"}
        packed = archive.extractfile("database.sql.gz").read()
        assert packed == database.read_bytes() and gzip.decompress(packed) == raw
        assert archive.extractfile("app/state.json").read() == b'{"fixture":true}'
    original = destination.read_bytes()
    with pytest.raises(FileExistsError):
        packager().package(database, app, destination)
    assert destination.read_bytes() == original


def test_packager_rejects_empty_database_before_creating_output(tmp_path):
    database = tmp_path / "empty.gz"
    database.touch()
    app = tmp_path / "app"
    app.mkdir()
    output = tmp_path / "output.tar.gz"
    with pytest.raises(ValueError):
        packager().package(database, app, output)
    assert not output.exists()


def test_shutdown_deadline_covers_backup_and_container_stop_budgets():
    stop = (DEPLOY / "stop.sh").read_text()
    start = (DEPLOY / "start.sh").read_text()
    kill_after, backup = map(int, re.search(r"timeout --kill-after=(\d+) (\d+)", stop).groups())
    app_stop = int(re.search(r"stop -t (\d+) app", stop)[1])
    db_stop = int(re.search(r"down --timeout (\d+)", stop)[1])
    outer = [int(n) for n in re.findall(r"TimeoutStartSec=(\d+)", start)]
    outer.append(int(re.search(r"TimeoutStartSec=(\d+)", (DEPLOY / "sessionquant-cost-guard.service").read_text())[1]))
    assert backup > 480
    assert len(outer) == 3 and all(limit > app_stop + backup + kill_after + db_stop + 60 for limit in outer)


@pytest.mark.skipif(os.name == "nt", reason="GCE Linux shell scripts")
@pytest.mark.parametrize("mode", ["timeout", "early_failure", "success"])
def test_shutdown_retains_failed_stage_and_never_reports_timeout_as_success(tmp_path, mode):
    root = tmp_path / "repo"
    deploy = root / "deploy/gcp"
    deploy.mkdir(parents=True)
    etc = tmp_path / "etc"
    etc.mkdir()
    status = etc / "backup-status"
    status.write_text("FAILED old phase=OLD\n")
    data = tmp_path / "database-disk"
    data.write_bytes(b"keep actual DB")
    calls = tmp_path / "calls"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable(
        bindir / "docker", 'echo "docker $*" >> "$CALLS"\nif [[ "$*" == *"--services db"* ]]; then echo db; fi\n'
    )
    executable(bindir / "systemctl", 'echo poweroff >> "$CALLS"\n')
    executable(bindir / "sync", 'echo sync >> "$CALLS"\n')
    if mode == "timeout":
        body = """printf 'RUNNING new phase=DUMP\n' > "$BACKUP_STATUS"
trap 'printf "FAILED new phase=DUMP exit_code=143\\n" > "$BACKUP_STATUS"; exit 143' TERM
sleep 5 &
wait
"""
    elif mode == "early_failure":
        body = "exit 2\n"
    else:
        body = 'printf "UPLOADED new complete.tar.gz\\n" > "$BACKUP_STATUS"\n'
    executable(deploy / "backup.sh", body)
    script = tmp_path / "stop.sh"
    script.write_text(
        (DEPLOY / "stop.sh")
        .read_text()
        .replace("/opt/sessionquant", str(root))
        .replace("/run/sessionquant-stop.lock", str(tmp_path / "stop.lock"))
        .replace("/etc/sessionquant", str(etc))
        .replace("/usr/bin/systemctl", str(bindir / "systemctl"))
        .replace("timeout --kill-after=30 1800", "timeout --kill-after=1 1")
    )
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": str(bindir) + ":" + os.environ["PATH"],
            "CALLS": str(calls),
            "BACKUP_STATUS": str(status),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == int(mode != "success")
    assert data.read_bytes() == b"keep actual DB"
    assert calls.read_text().splitlines()[-2:] == ["sync", "poweroff"]
    if mode == "timeout":
        assert "제한 시간" in result.stderr
        assert "phase=DUMP" in status.read_text() and "UPLOADED" not in status.read_text()
    elif mode == "early_failure":
        assert "phase=SHUTDOWN" in status.read_text() and "exit_code=2" in status.read_text()
    else:
        assert status.read_text().startswith("UPLOADED ")


@pytest.mark.skipif(os.name == "nt", reason="GCE Linux shell scripts")
@pytest.mark.parametrize("invalid", ["empty", "broken_gzip", "package_failure"])
def test_failed_verification_never_uploads_or_removes_previous_archive(tmp_path, invalid):
    import shutil

    root = tmp_path / "repo"
    deploy = root / "deploy/gcp"
    deploy.mkdir(parents=True)
    data = tmp_path / "data"
    (data / "app").mkdir(parents=True)
    backups = data / "backups"
    backups.mkdir()
    previous = backups / "sessionquant-previous.tar.gz"
    previous.write_bytes(b"keep previous")
    etc = tmp_path / "etc"
    etc.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable(
        bindir / "docker",
        'if [[ "$*" == *"--services app"* ]]; then exit 0; fi\n'
        + ("" if invalid == "empty" else 'echo "CREATE TABLE fixture (id INT);"\n'),
    )
    if invalid == "broken_gzip":
        executable(bindir / "gzip", 'if [[ "$1" == -1 ]]; then cat; else exec /usr/bin/gzip "$@"; fi\n')
    (deploy / "storage_check.py").write_text("pass")
    (deploy / "upload_backup.py").write_text('raise AssertionError("must never upload")')
    shutil.copyfile(DEPLOY / "package_backup.py", deploy / "package_backup.py")
    if invalid == "package_failure":
        (deploy / "package_backup.py").write_text("raise SystemExit(2)")
    script = tmp_path / "backup.sh"
    script.write_text(
        (DEPLOY / "backup.sh")
        .read_text()
        .replace("/opt/sessionquant", str(root))
        .replace("/var/lib/sessionquant", str(data))
        .replace("/etc/sessionquant", str(etc))
        .replace("/run/sessionquant-backup.lock", str(tmp_path / "backup.lock"))
    )
    result = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "PATH": str(bindir) + ":" + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert list(backups.iterdir()) == [previous] and previous.read_bytes() == b"keep previous"
    status = (etc / "backup-status").read_text()
    assert status.startswith("FAILED")
    assert ("phase=PACKAGE" if invalid == "package_failure" else "phase=VERIFY_SQL") in status
    assert "UPLOAD" not in status and "must never upload" not in result.stderr
