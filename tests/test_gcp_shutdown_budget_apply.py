"""Host-only installer tests; all paths and systemd calls use disposable doubles."""
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

fcntl = pytest.importorskip("fcntl")

from test_gcp_deployment import DEPLOY

spec = importlib.util.spec_from_file_location("shutdown_budget", DEPLOY / "apply_shutdown_budget.py")
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)
pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux deployment scripts")


def reviewed_payload(name):
    return Path(__file__).parent / "fixtures/shutdown_budget_20261120" / name


def old_body(name):
    body = reviewed_payload(name).read_text()
    if name in {"start.sh", "sessionquant-cost-guard.service"}:
        body = body.replace("TimeoutStartSec=4200", "TimeoutStartSec=2400")
        if name == "start.sh":
            clock_start = body.index("# Pin to this boot's monotonic clock.")
            clock_end = body.index("if ! delay=", clock_start)
            body = body[:clock_start] + "# Boot-scoped timer; an explicit service restart must not extend today's deadline.\n" + body[clock_end:]
            body = body.replace('    sq_close_usec=$(shutdown_usec 900)\n', '')
            body = body.replace('sq_close_usec=$(shutdown_usec "$((delay > 0 ? delay : 60))")\n', '')
            body = body.replace('--on-boot="${sq_close_usec}us"', '--on-active=900s', 1)
            body = body.replace('--on-boot="${sq_close_usec}us"', '--on-active="$((delay > 0 ? delay : 60))s"')
    elif name == "stop.sh":
        body = body.replace(
            "    # Reserve time inside the 70-minute service for app stop, a 45-minute\n"
            "    # backup, bounded cleanup, DB stop and disk flush. Never report timeout\n"
            "    # as a successful upload.\n",
            "    # Match the enclosing 40-minute systemd limit. A large logical dump may\n"
            "    # legitimately take more than the former eight-minute budget.\n")
        body = body.replace("2700", "1800").replace(
            "        # At most five passes within 600+15 seconds; each pass retains its\n"
            "        # own 90-second/row budget and verified-backup checks.\n",
            "        # At most 120+15 seconds, inside the enclosing 40-minute service budget.\n")
        body = body.replace("--kill-after=15 600", "--kill-after=15 120").replace("retention_after_update.sh", "retention.sh")
    elif name == "retention_after_update.sh":
        body = body.replace("Catch up after a successful backup", "Catch up after this update's backup")
        body = body.replace("이번 작업 직전", "업데이트 직전").replace("백업 후 자료 정리:", "업데이트 후 자료 정리:")
    assert patch.digest(body.encode()) == patch.PAYLOADS[name][0]
    return body.encode()


class Systemd:
    """Model systemd coldplug: reload rebases OnActiveSec, not OnBootSec.

    Unit activation timestamps survive a reload. This matches timer_coldplug /
    timer_enter_waiting and unit_notify in systemd 255/257, unlike the former
    constant-deadline double that masked the production failure.
    """
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.reloads = 0
        self.remaining = 3600_000_000
        self.monotonic = 1000_000_000_000
        self.started = 600_000_000
        self.base = {"market": "OnActiveUSec", "cost": "OnBootUSec"}
        self.values = {"market": 4000_000_000, "cost": 72000_000_000}
        self.scheduled = {"market": self.started + self.values["market"], "cost": self.values["cost"]}
        self.busy = False
        self.bad_timeout = False
        self.changed_deadline = False
        self.virtualization = "kvm"

    def __call__(self, args):
        self.calls.append(args)
        if args == ["systemd-detect-virt"]:
            return self.virtualization
        if args == ["systemctl", "daemon-reload"]:
            self.reloads += 1
            self.monotonic += 1_000_000_000
            pin = self.root / patch.TIMER_DROPIN
            if pin.exists():
                self.base["market"] = "OnBootUSec"
                self.values["market"] = int(re.search(rb"OnBootSec=(\d+)us", pin.read_bytes())[1])
            if self.base["market"] == "OnActiveUSec":
                self.scheduled["market"] = self.monotonic // 1000 + self.values["market"]
            else:
                self.scheduled["market"] = self.values["market"]
            self.scheduled["cost"] = self.values["cost"]
            if self.changed_deadline and self.reloads == 1:
                self.scheduled["cost"] += 1000
            return ""
        if args[:2] == ["systemctl", "show"]:
            unit, prop = args[2], args[3]
            if prop == "--property=ExecStart":
                return "{ path=/opt/sessionquant/deploy/gcp/stop.sh ; argv[]=/opt/sessionquant/deploy/gcp/stop.sh ; }"
            if prop == "--property=Unit":
                return unit.replace(".timer", ".service")
            if prop == "--property=ActiveState":
                return "active" if unit.endswith(".timer") or self.busy else "inactive"
        if args[:2] == ["busctl", "get-property"]:
            name = "cost" if "_2dcost_2dguard" in args[3] else "market"
            if self.reloads == 0 and self.remaining != 3600_000_000:
                self.values["market"] = self.monotonic // 1000 + self.remaining - self.started
                self.scheduled["market"] = self.started + self.values["market"]
            if args[-1] == "TimeoutStartUSec":
                return "t 2400000000" if self.bad_timeout else "t 4200000000"
            if args[-1] == "NextElapseUSecMonotonic":
                return "t " + str(self.scheduled[name])
            if args[-1] == "InactiveExitTimestampMonotonic":
                return "t " + str(self.started)
            if args[-1] == "WakeSystem":
                return "b false"
            if args[-1] == "Transient":
                return "b true" if name == "market" else "b false"
            if args[-1] == "RandomizedDelayUSec":
                return "t 0"
            if args[-1] == "TimersCalendar":
                return "a(sst) 0"
            if args[-1] == "TimersMonotonic":
                return f'a(stt) 1 "{self.base[name]}" {self.values[name]} {self.scheduled[name]}'
        raise AssertionError("unexpected command: " + repr(args))


@pytest.fixture
def installed(tmp_path):
    (tmp_path / "run").mkdir()
    payloads = {name: reviewed_payload(name).read_bytes() for name in patch.PAYLOADS}
    paths = {patch.REPO + name: old_body(name) for name in patch.PAYLOADS}
    paths.update({patch.REPO + name: (DEPLOY / name).read_bytes() for name in patch.DEPENDENCIES})
    paths["etc/systemd/system/sessionquant-cost-guard.service"] = old_body("sessionquant-cost-guard.service")
    for relative, body in paths.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o755 if path.suffix == ".sh" else 0o644)
    return tmp_path, payloads, paths, Systemd(tmp_path)


def apply(installed):
    root, payloads, _, systemd = installed
    return patch.apply(payloads, root=root, run_command=systemd, monotonic=lambda: systemd.monotonic)


def test_payload_hashes_match_reviewed_repository_files():
    patch.validate_payloads({name: reviewed_payload(name).read_bytes() for name in patch.PAYLOADS})
    for name, expected in patch.DEPENDENCIES.items():
        assert patch.digest((DEPLOY / name).read_bytes()) == expected


def test_live_install_preserves_timers_app_and_recovery_copy_and_is_repeatable(installed):
    root, payloads, before, systemd = installed
    backup = apply(installed)
    assert json.loads((backup / "manifest.json").read_text())["state"] == "APPLIED"
    assert backup.stat().st_mode & 0o777 == 0o700
    for relative, body in before.items():
        if (backup / relative).exists():
            assert (backup / relative).read_bytes() == body
    for name, body in payloads.items():
        path = root / patch.REPO / name
        assert path.read_bytes() == body
        assert path.stat().st_mode & 0o777 == (0o755 if path.suffix == ".sh" else 0o644)
    assert (root / patch.DROPIN).read_bytes() == patch.DROPIN_BODY
    assert patch.DROPIN.startswith("run/")  # today's override expires with the VM boot
    assert patch.TIMER_DROPIN.startswith("run/systemd/transient/")  # expires when the current timer is replaced
    assert (root / "etc/systemd/system/sessionquant-cost-guard.service").read_bytes() == payloads["sessionquant-cost-guard.service"]
    second = apply(installed)
    assert second != backup
    # Only reads + daemon reload; no start/restart, Docker, DB or timer rewrites.
    assert all(call[:2] in (["systemctl", "show"], ["systemctl", "daemon-reload"],
                           ["busctl", "get-property"], ["systemd-detect-virt"]) for call in systemd.calls)
    assert not list(root.rglob("*.timer"))
    assert not (root / "var/lib/sessionquant/mysql").exists()


@pytest.mark.parametrize("case", ["custom", "dependency", "symlink", "parent_symlink", "override", "payload"])
def test_rejects_unrecognized_files_before_any_host_write(installed, case):
    root, payloads, _, systemd = installed
    target = root / patch.REPO / "stop.sh"
    if case == "custom":
        target.write_bytes(b"custom edits must be preserved")
    elif case == "dependency":
        (root / patch.REPO / "retention.sh").write_bytes(b"unknown retention version")
    elif case == "symlink":
        target.unlink()
        target.symlink_to(root / patch.REPO / "start.sh")
    elif case == "parent_symlink":
        directory = root / "etc/systemd/system"
        directory.rename(directory.with_name("real-system"))
        directory.symlink_to(directory.with_name("real-system"), target_is_directory=True)
    elif case == "override":
        target = root / patch.DROPIN
        target.parent.mkdir(parents=True)
        target.write_bytes(b"custom systemd override")
    else:
        payloads["stop.sh"] += b"# not reviewed\n"
    with pytest.raises(ValueError):
        apply(installed)
    assert systemd.reloads == 0
    assert not (root / patch.BACKUP_ROOT).exists()


@pytest.mark.parametrize("case", ["busy", "near_deadline", "infinite", "locked", "maintenance", "container"])
def test_refuses_active_maintenance_or_imminent_shutdown(installed, case):
    root, _, before, systemd = installed
    stream = None
    if case == "busy":
        systemd.busy = True
    elif case == "near_deadline":
        systemd.remaining = 600_000_000
    elif case == "infinite":
        systemd.remaining = (1 << 64) - 1 - systemd.monotonic // 1000
    elif case == "container":
        systemd.virtualization = "docker"
    elif case == "maintenance":
        marker = root / "etc/sessionquant/maintenance"
        marker.parent.mkdir(parents=True)
        marker.touch()
    else:
        stream = (root / "run/sessionquant-backup.lock").open("a")
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ValueError):
            apply(installed)
    finally:
        if stream:
            stream.close()
    for relative, body in before.items():
        assert (root / relative).read_bytes() == body
    assert systemd.reloads == 0


@pytest.mark.parametrize("failure", ["timeout_override", "timer_changed", "write_error"])
def test_failed_apply_rolls_back_host_files(installed, monkeypatch, failure):
    root, _, before, systemd = installed
    if failure == "timeout_override":
        systemd.bad_timeout = True
    elif failure == "timer_changed":
        systemd.changed_deadline = True
    else:
        original_write = patch.atomic_write
        def fail_once(path, *args):
            if path == root / patch.DROPIN:
                raise OSError("injected write failure")
            original_write(path, *args)
        monkeypatch.setattr(patch, "atomic_write", fail_once)
    with pytest.raises((ValueError, OSError)):
        apply(installed)
    for relative, body in before.items():
        assert (root / relative).read_bytes() == body
    assert not (root / patch.DROPIN).exists()
    report, = (root / patch.BACKUP_ROOT).glob("*/manifest.json")
    assert json.loads(report.read_text())["state"] == "ROLLED_BACK_TIMER_PINNED"
    assert (root / patch.TIMER_DROPIN).read_bytes() == patch.timer_dropin(4600_000_000)
    assert systemd.scheduled == {"market": 4600_000_000, "cost": 72000_000_000}


def test_download_rejects_bad_revision_and_tampered_payload(monkeypatch):
    with pytest.raises(ValueError, match="커밋"):
        patch.download("main")
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            assert limit == 65537
            return b"unverified shell code"
    def urlopen(url, timeout):
        assert url.startswith("https://raw.githubusercontent.com/taegyoo1104/pennyquant/" + "a" * 40 + "/deploy/gcp/")
        assert timeout == 20
        return Response()
    monkeypatch.setattr(patch, "urlopen", urlopen)
    with pytest.raises(ValueError, match="검증 실패"):
        patch.download("a" * 40)


def test_failed_legacy_attempt_restores_original_exact_deadline(installed):
    root, _, _, systemd = installed
    original = {"sessionquant-market-close.timer": 4600_000_000 - 123,
                "sessionquant-cost-guard.timer": 72000_000_000}
    saved = root / patch.BACKUP_ROOT / "shutdown-budget-legacy" / "manifest.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"state": "ROLLED_BACK", "timer_deadlines": original}))
    # The failed installer reloaded once to apply and once to roll back files.
    # Neither reload restores the original OnActiveSec deadline by itself.
    systemd(["systemctl", "daemon-reload"])
    systemd(["systemctl", "daemon-reload"])
    assert systemd.scheduled["market"] > original["sessionquant-market-close.timer"]
    backup = apply(installed)
    assert systemd.scheduled["market"] == original["sessionquant-market-close.timer"]
    assert systemd.scheduled["cost"] == original["sessionquant-cost-guard.timer"]
    report = json.loads((backup / "manifest.json").read_text())
    assert report["timer_deadlines"] == original
    assert report["timer_before"]["sessionquant-market-close.timer"]["scheduled"] > original["sessionquant-market-close.timer"]
    # Future reloads, including unrelated maintenance, no longer postpone it.
    for _ in range(3):
        systemd(["systemctl", "daemon-reload"])
    assert systemd.scheduled["market"] == original["sessionquant-market-close.timer"]


def test_reported_production_drift_does_not_replace_the_original_close_deadline(installed):
    root, _, _, systemd = installed
    # User's Nov 20 diagnostic: the receipt was already later than the initial
    # arm, and the failed apply + rollback moved it another 17m32.924829s.
    systemd.monotonic = 11500_000_000_000
    systemd.started = 8_186_743_880
    systemd.values["market"] = 48_289_000_000
    systemd.scheduled["market"] = 59_288_452_183
    saved = root / patch.BACKUP_ROOT / "shutdown-budget-production" / "manifest.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"state": "ROLLED_BACK", "timer_deadlines": {
        "sessionquant-market-close.timer": 58_235_527_354,
        "sessionquant-cost-guard.timer": 72_000_000_000}}))
    apply(installed)
    assert systemd.scheduled["market"] == 56_475_743_880  # original activation + 13h24m49s
    assert systemd.scheduled["cost"] == 72_000_000_000


@pytest.mark.parametrize("saved", [None, [], {"state": "ROLLED_BACK", "timer_deadlines": {
    "sessionquant-market-close.timer": 9000_000_000, "sessionquant-cost-guard.timer": 72000_000_000}}])
def test_recovery_uses_current_boot_anchor_without_trusting_unrelated_old_receipt(installed, saved):
    root, _, _, systemd = installed
    if saved is not None:
        path = root / patch.BACKUP_ROOT / "shutdown-budget-other-boot" / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(saved))
    systemd(["systemctl", "daemon-reload"])
    assert systemd.scheduled["market"] != 4600_000_000
    apply(installed)
    assert systemd.scheduled == {"market": 4600_000_000, "cost": 72000_000_000}


def test_timer_pin_write_failure_does_not_reload_or_change_other_host_files(installed, monkeypatch):
    root, _, before, systemd = installed
    original_write = patch.atomic_write
    def fail_pin(path, *args):
        if path == root / patch.TIMER_DROPIN:
            raise OSError("cannot write timer override")
        original_write(path, *args)
    monkeypatch.setattr(patch, "atomic_write", fail_pin)
    with pytest.raises(OSError, match="timer override"):
        apply(installed)
    assert systemd.reloads == 0
    for relative, body in before.items():
        assert (root / relative).read_bytes() == body
    assert not (root / patch.TIMER_DROPIN).exists()


@pytest.mark.parametrize("property_name,value", [
    ("Transient", "b false"), ("WakeSystem", "b true"), ("RandomizedDelayUSec", "t 1000000"),
    ("TimersCalendar", 'a(sst) 1 "OnCalendar" "*-*-* 21:05:00" 1795208700000000'),
    ("TimersMonotonic", 'a(stt) 2 "OnActiveUSec" 4000000000 4600000000 "OnBootUSec" 4600000000 4600000000'),
])
def test_custom_timer_semantics_are_not_replaced(installed, property_name, value):
    root, payloads, before, systemd = installed
    def custom(args):
        return value if args[:2] == ["busctl", "get-property"] and args[-1] == property_name else systemd(args)
    with pytest.raises(ValueError):
        patch.apply(payloads, root=root, run_command=custom, monotonic=lambda: systemd.monotonic)
    assert systemd.reloads == 0
    for relative, body in before.items():
        assert (root / relative).read_bytes() == body
