"""Startup handoffs authorize only one unchanged, live update operation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


DEPLOY = Path(__file__).parents[1] / "deploy" / "gcp"
pytestmark = pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/stat").exists(),
    reason="Linux /proc process identity and flock fixture",
)


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("startup_handoff", DEPLOY / "startup_handoff.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, relative in {
        "ROOT": "repo", "ETC": "etc", "DATA": "health", "APP_DATA": "app",
        "MARKER": "prepared.json", "UPDATE_LOCK": "update.lock",
    }.items():
        monkeypatch.setattr(module, name, tmp_path / relative)
    # CI is unprivileged. Simulate only the marker's owner; use real mode,
    # symlink, process and lock checks for all other security boundaries.
    class MarkerPath(type(tmp_path)):
        owner = 0

        def lstat(self):
            values = list(super().lstat())
            values[4] = self.owner
            return os.stat_result(values)

    monkeypatch.setattr(module, "MARKER", MarkerPath(module.MARKER))
    for directory in (module.ROOT, module.ETC, module.DATA, module.APP_DATA):
        directory.mkdir()
    for name in ("app.env", "db.env", "backup-status"):
        (module.ETC / name).write_text("fixture " + name)
    (module.DATA / "startup-retention-last.json").write_text(json.dumps({"status": "PENDING"}))
    (module.APP_DATA / "selected-policy.yaml").write_text("retention_days: 30\n")
    state = {"revision": "a" * 40, "image": "sha256:reviewed"}

    def output(command, **kwargs):
        assert kwargs == {"text": True, "timeout": 10}
        if command[0] == "git":
            assert command == ["git", "-C", str(module.ROOT), "rev-parse", "HEAD"]
            return state["revision"] + "\n"
        assert command == ["docker", "image", "inspect", "sessionquant:local", "--format", "{{.Id}}"]
        return state["image"] + "\n"

    monkeypatch.setattr(module.subprocess, "check_output", output)
    # A separate live updater owns a real fd 9 flock. A second pipe message can
    # release the lock without ending its process, exercising both requirements.
    updater = subprocess.Popen(
        ["bash", "-c", 'exec 9>"$1"\nflock -n 9 || exit 1\nprintf "ready\\n"\n'
         'read -r command\nflock -u 9\nprintf "unlocked\\n"\nread -r command',
         "fixture-updater", str(module.UPDATE_LOCK)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert updater.stdout.readline() == "ready\n"
    monkeypatch.setattr(module.os, "getppid", lambda: updater.pid)
    try:
        yield module, updater, state
    finally:
        updater.stdin.close()
        updater.wait(timeout=5)
        updater.stdout.close()


@pytest.mark.parametrize("status", ["CLEAR", "PENDING", "UNKNOWN", "DISABLED"])
def test_current_update_handoff_is_private_and_consumed_exactly_once(handoff, status, capsys):
    module, updater, _ = handoff
    report = {"status": status}
    if status == "PENDING":
        report["maintenance"] = {"status": "PARTIAL", "batches": 2}
    (module.DATA / "startup-retention-last.json").write_text(json.dumps(report))
    assert module.updater_holds_lock(updater.pid)
    module.create()
    assert module.MARKER.stat().st_mode & 0o777 == 0o600
    assert json.loads(module.MARKER.read_text())["pid"] == updater.pid
    assert module.consume()
    assert status in capsys.readouterr().out
    assert not module.MARKER.exists()
    assert not module.consume()


@pytest.mark.parametrize("origin", [1.0, 1000000.0])
@pytest.mark.parametrize("age,allowed", [(-1, False), (0, True), (120, True), (121, False)])
def test_handoff_expiry_depends_on_elapsed_time_not_clock_origin(handoff, monkeypatch, origin, age, allowed):
    module, _, _ = handoff
    clock = [origin]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    module.create()
    clock[0] += age
    assert module.consume() is allowed
    assert not module.MARKER.exists()


@pytest.mark.parametrize("status", ["FAILED", "COMPLETE", "PARTIAL", None])
def test_handoff_creation_requires_a_completed_startup_probe(handoff, status):
    module, _, _ = handoff
    (module.DATA / "startup-retention-last.json").write_text(json.dumps({"status": status}))
    with pytest.raises(ValueError):
        module.create()
    assert not module.MARKER.exists()


@pytest.mark.parametrize("changed", ["app.env", "db.env", "backup-status", "report", "revision", "image",
                                     "policy", "added-app-file", "deleted-app-file"])
def test_changed_input_invalidates_and_consumes_handoff(handoff, changed):
    module, _, state = handoff
    module.create()
    if changed in state:
        state[changed] += "-changed"
    elif changed == "report":
        (module.DATA / "startup-retention-last.json").write_text('{"status":"CLEAR"}')
    elif changed == "policy":
        (module.APP_DATA / "selected-policy.yaml").write_text("retention_days: 5\n")
    elif changed == "added-app-file":
        (module.APP_DATA / "another-policy.yaml").write_text("retention_days: 5\n")
    elif changed == "deleted-app-file":
        (module.APP_DATA / "selected-policy.yaml").unlink()
    else:
        (module.ETC / changed).write_text("changed after preparation")
    assert not module.consume()
    assert not module.MARKER.exists()


@pytest.mark.parametrize("invalid", ["symlink", "fifo", "oversized"])
def test_creation_refuses_unbounded_or_redirected_mounted_app_files(handoff, invalid):
    module, _, _ = handoff
    path = module.APP_DATA / "unverifiable"
    if invalid == "symlink":
        path.symlink_to(module.ETC / "app.env")
    elif invalid == "fifo":
        os.mkfifo(path)
    else:
        with path.open("wb") as stream:
            stream.truncate(256 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        module.create()
    assert not module.MARKER.exists()


@pytest.mark.parametrize("changed", ["expired", "future", "previous-boot", "reused-pid", "missing-field"])
def test_stale_or_incomplete_handoff_cannot_skip_startup_check(handoff, monkeypatch, changed):
    module, _, _ = handoff
    module.create()
    payload = json.loads(module.MARKER.read_text())
    if changed == "expired":
        monkeypatch.setattr(module.time, "monotonic", lambda: payload["at"] + 121)
    elif changed == "future":
        monkeypatch.setattr(module.time, "monotonic", lambda: payload["at"] - 1)
    elif changed == "previous-boot":
        payload["boot"] = "another-boot"
    elif changed == "reused-pid":
        payload["process"] = "another-process-start-time"
    else:
        del payload["fingerprint"]
    module.MARKER.write_text(json.dumps(payload))
    assert not module.consume()
    assert not module.MARKER.exists()


@pytest.mark.parametrize("updater_state", ["exited", "unlocked"])
def test_prepared_handoff_requires_updater_still_alive_and_locked(handoff, updater_state):
    module, updater, _ = handoff
    module.create()
    updater.stdin.write("release\n")
    updater.stdin.flush()
    assert updater.stdout.readline() == "unlocked\n"
    if updater_state == "exited":
        updater.stdin.write("exit\n")
        updater.stdin.flush()
        updater.wait(timeout=5)
    else:
        assert updater.poll() is None
        assert not module.updater_holds_lock(updater.pid)
    assert not module.consume()
    assert not module.MARKER.exists()


def test_creation_refuses_live_parent_without_update_lock(handoff):
    module, updater, _ = handoff
    updater.stdin.write("release\n")
    updater.stdin.flush()
    assert updater.stdout.readline() == "unlocked\n"
    assert updater.poll() is None
    with pytest.raises(ValueError):
        module.create()
    assert not module.MARKER.exists()


@pytest.mark.parametrize("invalid", ["mode", "owner", "symlink", "malformed", "missing-input", "image-unavailable"])
def test_invalid_or_unverifiable_marker_falls_back_to_startup_check(handoff, monkeypatch, invalid):
    module, _, _ = handoff
    module.create()
    if invalid == "mode":
        module.MARKER.chmod(0o644)
    elif invalid == "owner":
        monkeypatch.setattr(type(module.MARKER), "owner", 1)
    elif invalid == "symlink":
        real_marker = module.MARKER.with_suffix(".real")
        module.MARKER.rename(real_marker)
        module.MARKER.symlink_to(real_marker)
    elif invalid == "malformed":
        module.MARKER.write_text("invalid json")
    elif invalid == "missing-input":
        (module.ETC / "app.env").unlink()
    else:
        def unavailable(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "docker")
        monkeypatch.setattr(module.subprocess, "check_output", unavailable)
    assert not module.consume()
