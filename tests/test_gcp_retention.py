"""Disposable command doubles: no VM, production files or shutdown calls."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from test_gcp_deployment import DEPLOY, executable

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux deployment scripts")


@pytest.mark.parametrize('statuses,expected,success', [
    (['COMPLETE'], 1, True), (['DISABLED'], 1, True),
    (['PARTIAL', 'COMPLETE'], 2, True), (['PARTIAL']*7, 5, True),
    (['PRESENT', 'COMPLETE'], 2, True), (['PARTIAL', 'FAIL'], 2, False),
    (['INVALID'], 1, False), (['QUERY_TIMEOUT'], 1, False),
])
def test_update_catchup_is_bounded_and_stops_on_errors(tmp_path, statuses, expected, success):
    root = tmp_path/'repo'
    deploy = root/'deploy/gcp'
    deploy.mkdir(parents=True)
    data = tmp_path/'data'
    data.mkdir()
    calls = tmp_path/'calls'
    worker = tmp_path/'worker.py'
    worker.write_text('''import json, os, pathlib, sys
calls=pathlib.Path(os.environ['CALLS'])
history=calls.read_text().splitlines() if calls.exists() else []
status=json.loads(os.environ['STATUSES'])[len(history)]
with calls.open('a') as stream: stream.write(sys.argv[1]+'\\n')
if status=='FAIL': raise SystemExit(42)
report={'status':'COMPLETE' if status=='PRESENT' else status, 'dry_run':False,
        'observability':{'backlog_status':'PRESENT' if status=='PRESENT' else 'UNKNOWN'}}
if status=='QUERY_TIMEOUT':
    report.update(status='PARTIAL', errors=[{'table':'pq_research_events','code':3024}])
pathlib.Path(os.environ['REPORT']).write_text(json.dumps(report))
''')
    executable(deploy/'retention.sh', 'python3 "$WORKER" "$1"\n')
    script = tmp_path/'catchup.sh'
    script.write_text((DEPLOY/'retention_after_update.sh').read_text().replace('/opt/sessionquant', str(root))
                      .replace('/var/lib/sessionquant/app', str(data)))
    result = subprocess.run(['bash', str(script), 'FAILED prior backup'], capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'CALLS':str(calls), 'WORKER':str(worker),
                                 'REPORT':str(data/'retention-last.json'), 'STATUSES':json.dumps(statuses)})
    assert (result.returncode == 0) is success, result.stderr
    assert calls.read_text().splitlines() == ['FAILED prior backup']*expected
    assert ('5회 한도' in result.stdout) is (expected == 5)


def setup_wrapper(tmp_path, *, current="UPLOADED new backup.tar.gz", app_running=False, mode="COMPLETE",
                  app_query_exit=0, observability=None):
    root = tmp_path / "repo"
    root.mkdir()
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "backup-status").write_text(current)
    data = tmp_path / "app"
    data.mkdir()
    (data / "retention-last.json").write_text('{"previous":"keep"}')
    calls = tmp_path / "calls"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable(bindir / "chown", "exit 0\n")
    report = json.dumps({"status": mode, "dry_run": False, "deleted": 20, "scanned": 25,
                         "observability": observability or {}})
    action = "exit 2" if mode == "FAIL" else "sleep 5" if mode == "WAIT" else "printf '%s\\n' '" + report + "'"
    executable(bindir / "docker", f'''echo "docker $*" >> "$CALLS"
if [[ "$*" == *"--services app"* ]]; then
    {"echo app" if app_running else ":"}
    exit {app_query_exit}
elif [[ "$*" == *" run "* ]]; then
    {action}
fi
''')
    script = tmp_path / "retention.sh"
    script.write_text((DEPLOY / "retention.sh").read_text().replace("/opt/sessionquant", str(root))
                      .replace("/etc/sessionquant", str(etc)).replace("/var/lib/sessionquant/app", str(data)))
    env = {**os.environ, "PATH": str(bindir) + ":" + os.environ["PATH"], "CALLS": str(calls)}
    return script, env, etc, data, calls


@pytest.mark.parametrize("current,previous,running", [
    ("FAILED new", "UPLOADED old backup.tar.gz", False),
    ("UPLOADED old backup.tar.gz", "UPLOADED old backup.tar.gz", False),
    ("UPLOADED new backup.tar.gz", "MISSING", True),
])
def test_cleanup_refuses_failed_or_unchanged_backup_and_running_app(tmp_path, current, previous, running):
    script, env, etc, data, calls = setup_wrapper(tmp_path, current=current, app_running=running)
    result = subprocess.run(["bash", str(script), previous], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert not calls.exists() or " run " not in calls.read_text()
    assert json.loads((data / "retention-last.json").read_text()) == {"previous": "keep"}
    assert (etc / "backup-status").read_text() == current


def test_cleanup_refuses_failed_app_status_query_before_starting_deletion(tmp_path):
    script, env, etc, data, calls = setup_wrapper(tmp_path, app_query_exit=42)
    result = subprocess.run(["bash", str(script), "UPLOADED old backup.tar.gz"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    lines = calls.read_text().splitlines()
    assert len(lines) == 1 and "ps --status running --services app" in lines[0]
    assert json.loads((data / "retention-last.json").read_text()) == {"previous": "keep"}
    assert (etc / "backup-status").read_text() == "UPLOADED new backup.tar.gz"
    assert not list(data.glob(".retention*"))
    assert "실행 상태 조회 실패" in result.stderr


@pytest.mark.parametrize("mode", ["COMPLETE", "PARTIAL", "DISABLED", "FAIL"])
def test_cleanup_preserves_backup_and_removes_only_its_own_container(tmp_path, mode):
    script, env, etc, data, calls = setup_wrapper(tmp_path, mode=mode)
    result = subprocess.run(["bash", str(script), "UPLOADED old backup.tar.gz"], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == (2 if mode == "FAIL" else 0), result.stderr
    assert (etc / "backup-status").read_text() == "UPLOADED new backup.tar.gz"
    lines = calls.read_text().splitlines()
    run = next(line for line in lines if " run " in line)
    name = run.split("--name ")[1].split()[0]
    assert name.startswith("sessionquant-retention-")
    assert lines[-1] == "docker rm -f " + name
    assert "--volume" in run and ":ro app sh -c exec python -m sessionquant data-retention" in run
    assert ".retention-proof." in run and str(etc) not in run
    assert "--apply --backup-receipt" in run
    assert not list(data.glob(".retention.*"))
    assert not list(data.glob(".retention-proof.*"))
    report = json.loads((data / "retention-last.json").read_text())
    assert (etc / "retention-status").read_text().startswith("FAILED" if mode == "FAIL" else mode)
    assert report == {"previous": "keep"} if mode == "FAIL" else report["status"] == mode


def test_timeout_removes_the_oneoff_container_and_keeps_last_report(tmp_path):
    script, env, etc, data, calls = setup_wrapper(tmp_path, mode="WAIT")
    result = subprocess.run(["timeout", "--kill-after=2", "0.5", "bash", str(script), "old"],
                            env=env, capture_output=True, text=True, timeout=8)
    assert result.returncode == 124
    lines = calls.read_text().splitlines()
    name = next(line for line in lines if " run " in line).split("--name ")[1].split()[0]
    assert lines[-1] == "docker rm -f " + name
    assert "FAILED" in (etc / "retention-status").read_text()
    assert (etc / "backup-status").read_text().startswith("UPLOADED")
    assert json.loads((data / "retention-last.json").read_text()) == {"previous": "keep"}
    assert not list(data.glob(".retention*"))


def test_cleanup_status_keeps_backlog_warning_separate_from_completed_pass(tmp_path):
    observation = {"backlog_status": "PRESENT", "attention": "CAPACITY_REVIEW"}
    script, env, etc, data, calls = setup_wrapper(tmp_path, observability=observation)
    result = subprocess.run(["bash", str(script), "UPLOADED old backup.tar.gz"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    status = (etc / "retention-status").read_text()
    assert status.startswith("COMPLETE ") and "backlog=PRESENT attention=CAPACITY_REVIEW" in status
    assert json.loads((data / "retention-last.json").read_text())["observability"] == observation


def test_failed_cleanup_still_stops_vm_and_keeps_successful_backup(tmp_path):
    root = tmp_path / "repo"
    deploy = root / "deploy/gcp"
    deploy.mkdir(parents=True)
    etc = tmp_path / "etc"
    etc.mkdir()
    status = etc / "backup-status"
    status.write_text("UPLOADED old\n")
    calls = tmp_path / "calls"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable(bindir / "docker", 'echo "docker $*" >> "$CALLS"\nif [[ "$*" == *"--services db"* ]]; then echo db; fi\n')
    executable(bindir / "systemctl", 'echo poweroff >> "$CALLS"\n')
    executable(bindir / "sync", 'echo sync >> "$CALLS"\n')
    executable(deploy / "backup.sh", 'echo "UPLOADED new complete.tar.gz" > "$BACKUP_STATUS"\n')
    executable(deploy / "retention.sh", 'echo retention >> "$CALLS"\nexit 124\n')
    script = tmp_path / "stop.sh"
    script.write_text((DEPLOY / "stop.sh").read_text().replace("/opt/sessionquant", str(root))
                      .replace("/run/sessionquant-stop.lock", str(tmp_path / "lock"))
                      .replace("/etc/sessionquant", str(etc)).replace("/usr/bin/systemctl", str(bindir / "systemctl")))
    result = subprocess.run(["bash", str(script)], env={**os.environ, "PATH": str(bindir) + ":" + os.environ["PATH"],
                            "CALLS": str(calls), "BACKUP_STATUS": str(status)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and status.read_text().startswith("UPLOADED new")
    assert "exit_code=124" in (etc / "retention-status").read_text()
    assert calls.read_text().splitlines()[-2:] == ["sync", "poweroff"]
    assert "down --timeout 120" in calls.read_text()
