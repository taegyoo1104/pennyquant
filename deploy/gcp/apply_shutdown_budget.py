#!/usr/bin/env python3
"""Apply the reviewed shutdown-budget fix without restarting the app or timers."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import tempfile
import time
from urllib.request import urlopen


# Only these four small, reviewed host files can be installed. A revision pins
# downloads; independent content hashes also reject unrelated/future changes.
PAYLOADS = {
    "start.sh": ("7f1b9e92524ccf7852a588ba6e3115a765de8a46e432cf255d75515b7153ae44",
                 "8e7b7a1d8a5fecd8b0e18862e401cdcfbb81c285a8269a37f2e28d547df38f1a",
                 "90f004ddd0a7153431dab16c17e887d875c2c6025e003c5358a2a3811bcb52be"),
    "stop.sh": ("5ca60b2205e488313771d930fcdfb98daa734c60908c6b217a7e90bca4a864cc",
                "f669b9109c608f144ce0d368d1c38cc3b4eef6dad340f42e5d4a866d73eafc10"),
    "sessionquant-cost-guard.service": ("0120f1edd9eee4dfe44a619a22aa9c1984d95f109b5961e53b6f34e53acc222c",
                                        "9a3a4ddc6340514ae744a5d8952fe46550eda6d40cdca5385016e7e84bec28ff"),
    "retention_after_update.sh": ("a09064e2cf99820f1b8b91c36826d18d011a0f588ec0757b9e1216bc95adf0b1",
                                  "691e39d71e3fdaa5832139e883a3be6a2d94abf0f26f66967f14b5de838aec18"),
}
DEPENDENCIES = {
    "retention.sh": "d4f3aae3532af632aea8f51d12ae2322cf9fd1a212c1230c85a32952b20777d0",
    "backup.sh": "2e6368166592e5a7a0e0b5faeb9e55959b0f194b793fda6397d30b70a2fa67fd",
}
SERVICES = ("sessionquant-market-close", "sessionquant-cost-guard")
# The override is only for today's already-created transient service. Next boot
# uses the updated start.sh, so no persistent override can outlive this fix.
DROPIN = "run/systemd/system/sessionquant-market-close.service.d/90-sessionquant-backup-budget.conf"
DROPIN_BODY = b"[Service]\nTimeoutStartSec=4200\n"
# Belongs to this transient timer, not merely this boot. systemd removes it
# when start.sh replaces the unit, so an old pin cannot override a newly armed
# holiday/calendar-failure deadline during a later service start.
TIMER_DROPIN = "run/systemd/transient/sessionquant-market-close.timer.d/90-sessionquant-fixed-deadline.conf"
REPO = "opt/sessionquant/deploy/gcp/"
BACKUP_ROOT = "var/lib/sessionquant/update-backups"
MIN_REMAINING_USEC = 600 * 1_000_000


def digest(body):
    return hashlib.sha256(body).hexdigest()


def validate_payloads(payloads):
    if set(payloads) != set(PAYLOADS):
        raise ValueError("종료 설정 파일 목록이 일치하지 않습니다.")
    for name, body in payloads.items():
        if len(body) > 65536 or digest(body) != PAYLOADS[name][-1]:
            raise ValueError("내려받은 파일 검증 실패: " + name)


def download(revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("검토한 전체 커밋 SHA가 필요합니다.")
    payloads = {}
    for name in PAYLOADS:
        url = f"https://raw.githubusercontent.com/taegyoo1104/pennyquant/{revision}/deploy/gcp/{name}"
        with urlopen(url, timeout=20) as response:
            payloads[name] = response.read(65537)
    validate_payloads(payloads)
    return payloads


def run(args):
    return subprocess.check_output(args, text=True, timeout=10).strip()


def property_tokens(run_command, unit, interface, name):
    escaped = unit.replace("-", "_2d").replace(".", "_2e")
    value = run_command(["busctl", "get-property", "org.freedesktop.systemd1",
                         "/org/freedesktop/systemd1/unit/" + escaped,
                         "org.freedesktop.systemd1." + interface, name])
    return shlex.split(value)


def property_usec(run_command, unit, interface, name):
    tokens = property_tokens(run_command, unit, interface, name)
    if len(tokens) != 2 or tokens[0] != "t" or not tokens[1].isdigit():
        raise ValueError("종료 예약 값을 확인할 수 없습니다: " + unit)
    return int(tokens[1])


def timer_snapshots(run_command, monotonic):
    result = {}
    for name in SERVICES:
        service, timer = name + ".service", name + ".timer"
        def show(unit, prop):
            return run_command(["systemctl", "show", unit, "--property=" + prop, "--value"])
        if show(service, "ActiveState") not in {"inactive", "failed"}:
            raise ValueError("종료 작업이 진행 중입니다: " + service)
        if "/opt/sessionquant/deploy/gcp/stop.sh" not in show(service, "ExecStart"):
            raise ValueError("알려진 종료 작업이 아닙니다: " + service)
        if show(timer, "ActiveState") != "active" or show(timer, "Unit") != service:
            raise ValueError("활성 종료 예약을 확인할 수 없습니다: " + timer)
        if name == "sessionquant-market-close" and property_tokens(run_command, timer, "Unit", "Transient") != ["b", "true"]:
            raise ValueError("현재 실행에 등록된 임시 장 마감 타이머가 아닙니다.")
        if (property_tokens(run_command, timer, "Timer", "WakeSystem") != ["b", "false"] or
                property_usec(run_command, timer, "Timer", "RandomizedDelayUSec") != 0 or
                property_tokens(run_command, timer, "Timer", "TimersCalendar") != ["a(sst)", "0"]):
            raise ValueError("지원하지 않는 종료 예약 설정입니다: " + timer)
        rule = property_tokens(run_command, timer, "Timer", "TimersMonotonic")
        if (len(rule) != 5 or rule[:2] != ["a(stt)", "1"] or
                rule[2] not in {"OnActiveUSec", "OnBootUSec"} or not all(n.isdigit() for n in rule[3:])):
            raise ValueError("종료 예약의 기준을 확인할 수 없습니다: " + timer)
        if name == "sessionquant-cost-guard" and rule[2] != "OnBootUSec":
            raise ValueError("비용 제한 예약이 부팅 기준이 아닙니다.")
        value, scheduled = int(rule[3]), property_usec(run_command, timer, "Timer", "NextElapseUSecMonotonic")
        if scheduled != int(rule[4]):
            raise ValueError("종료 예약 조회 중 값이 바뀌었습니다: " + timer)
        if rule[2] == "OnBootUSec" and scheduled != value:
            raise ValueError("부팅 기준 종료 예약에 알 수 없는 시간 차이가 있습니다.")
        started = 0
        if rule[2] == "OnActiveUSec":
            # systemd preserves this timestamp on reload, but recomputes an
            # OnActiveSec deadline from *now* while coldplugging the timer.
            started = property_usec(run_command, timer, "Unit", "InactiveExitTimestampMonotonic")
            if not 0 < started <= monotonic() // 1000:
                raise ValueError("최초 종료 예약 등록 시각을 확인할 수 없습니다.")
        original = started + value
        if original >= (1 << 64) - 1 or original - monotonic() // 1000 <= MIN_REMAINING_USEC:
            raise ValueError("종료 예약까지 10분 이상 남아 있어야 합니다: " + timer)
        if scheduled < original - 1_000_000:
            raise ValueError("원래 시각보다 이른 종료 예약이 있어 직접 확인이 필요합니다.")
        result[timer] = {"base": rule[2], "value": value, "started": started,
                         "scheduled": scheduled, "original": original}
    return result


def original_deadlines(root, snapshots):
    targets = {name: row["original"] for name, row in snapshots.items()}
    # The first installer saved exact deadlines but not a boot ID. Only accept
    # a saved value if it agrees within one second with this boot's independent
    # activation timestamp + duration. The activation timestamp is measured
    # just after the initial timer arm. Never restore an arbitrary old deadline.
    candidates = sorted((root / BACKUP_ROOT).glob("shutdown-budget-*/manifest.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:10]
    for path in candidates:
        try:
            saved = json.loads(snapshot(path)[0])
            if not isinstance(saved, dict):
                continue
            values = saved.get("timer_deadlines", {})
            if (saved.get("state") not in {"APPLIED", "ROLLED_BACK", "ROLLED_BACK_TIMER_PINNED"} or
                    set(values) != set(targets) or
                    any(type(values[name]) is not int or abs(values[name] - target) > 1_000_000
                        for name, target in targets.items())):
                continue
            # Boot-based values are already exact and cannot use this tolerance.
            if any(row["base"] == "OnBootUSec" and values[name] != row["original"]
                   for name, row in snapshots.items()):
                continue
            return values
        except (ValueError, OSError, TypeError):
            continue
    return targets


def timer_dropin(deadline):
    # An empty timer expression clears earlier relative/calendar expressions.
    return f"[Timer]\nOnActiveSec=\nOnBootSec={deadline}us\n".encode()


def verify_deadlines(run_command, monotonic, targets):
    actual = timer_snapshots(run_command, monotonic)
    if any(row["base"] != "OnBootUSec" or row["scheduled"] != targets[name]
           for name, row in actual.items()):
        raise ValueError("원래 종료 예약 시각을 확인하지 못했습니다.")


def no_symlinks(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("심볼릭 링크 경로는 수정하지 않습니다: " + str(path))


def snapshot(path, *, optional=False):
    no_symlinks(path)
    if optional and not path.exists():
        return None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise ValueError("작은 일반 파일이 아닙니다: " + str(path))
    return path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid


def atomic_write(path, body, mode, uid, gid):
    no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sq-shutdown-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply(payloads, *, root=Path("/"), run_command=run, monotonic=time.monotonic_ns):
    validate_payloads(payloads)
    # Network downloads complete before acquiring locks. A due timer must never
    # be held off while this installer waits on network access.
    with ExitStack() as stack:
        for name in ("update", "stop", "backup"):
            path = root / f"run/sessionquant-{name}.lock"
            no_symlinks(path)
            stream = stack.enter_context(path.open("a"))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("다른 업데이트·백업·종료 작업이 실행 중입니다.") from None
        for marker in ("maintenance", "storage-blocked"):
            if (root / "etc/sessionquant" / marker).exists():
                raise ValueError("기존 점검·저장 공간 차단을 먼저 확인하세요.")
        # OnBootSec is an absolute CLOCK_MONOTONIC deadline on a VM. Container
        # managers offset it by their own start time, so do not apply there.
        if run_command(["systemd-detect-virt"]) not in {"kvm", "qemu", "google"}:
            raise ValueError("이 도구는 GCP 운영 VM에서 실행해야 합니다.")
        before_timers = timer_snapshots(run_command, monotonic)
        before_deadlines = original_deadlines(root, before_timers)
        if min(before_deadlines.values()) - monotonic() // 1000 <= MIN_REMAINING_USEC:
            raise ValueError("원래 종료 예약까지 10분 이상 남아 있어야 합니다.")
        for name, expected in DEPENDENCIES.items():
            if digest(snapshot(root / REPO / name)[0]) != expected:
                raise ValueError("지원하는 백업·정리 버전이 아닙니다: " + name)
        pinned_timer = timer_dropin(before_deadlines["sessionquant-market-close.timer"])
        changes = {TIMER_DROPIN: pinned_timer, **{REPO + name: body for name, body in payloads.items()}}
        changes["etc/systemd/system/sessionquant-cost-guard.service"] = payloads["sessionquant-cost-guard.service"]
        changes[DROPIN] = DROPIN_BODY
        original = {}
        for relative in changes:
            old = snapshot(root / relative, optional=relative in {DROPIN, TIMER_DROPIN})
            if relative in {DROPIN, TIMER_DROPIN}:
                valid = old is None or old[0] == changes[relative]
            else:
                valid = digest(old[0]) in PAYLOADS[Path(relative).name]
            if not valid:
                raise ValueError("별도 수정이 있어 보존하고 중단합니다: " + relative)
            original[relative] = old
        backup_root = root / BACKUP_ROOT
        no_symlinks(backup_root)
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = Path(tempfile.mkdtemp(prefix="shutdown-budget-", dir=backup_root))
        for relative, old in original.items():
            if old is not None:
                atomic_write(backup / relative, old[0], 0o600, os.geteuid(), os.getegid())
        manifest = {"version": 2, "state": "PREPARED", "timer_deadlines": before_deadlines,
                    "timer_before": before_timers, "files": {
            name: None if old is None else {"sha256": digest(old[0]), "mode": old[1], "uid": old[2], "gid": old[3]}
            for name, old in original.items()}}
        def record(state):
            manifest["state"] = state
            atomic_write(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode(),
                         0o600, os.geteuid(), os.getegid())
        record("PREPARED")
        # Recheck before the first write; no long command runs while files are
        # partially installed. Timers retain their original activation dates.
        if timer_snapshots(run_command, monotonic) != before_timers:
            raise ValueError("종료 예약이 바뀌어 적용하지 않았습니다.")
        for relative, old in original.items():
            if snapshot(root / relative, optional=old is None) != old:
                raise ValueError("준비 중 파일이 바뀌었습니다: " + relative)
        written = []
        try:
            for relative, body in changes.items():
                old = original[relative]
                if old is not None and old[0] == body:
                    continue
                mode, uid, gid = old[1:] if old is not None else (0o644, os.geteuid(), os.getegid())
                written.append(relative)
                atomic_write(root / relative, body, mode, uid, gid)
            run_command(["systemctl", "daemon-reload"])
            for name in SERVICES:
                if property_usec(run_command, name + ".service", "Service", "TimeoutStartUSec") != 4200_000_000:
                    raise ValueError("종료 서비스 제한 적용을 확인하지 못했습니다: " + name)
            verify_deadlines(run_command, monotonic, before_deadlines)
            record("APPLIED")
        except BaseException:
            try:
                for relative in reversed(written):
                    if relative == TIMER_DROPIN:
                        continue  # Restoring OnActiveSec would postpone it again.
                    old = original[relative]
                    if old is None:
                        (root / relative).unlink(missing_ok=True)
                    else:
                        atomic_write(root / relative, *old)
                pin = snapshot(root / TIMER_DROPIN, optional=True)
                if pin is not None and pin[0] == pinned_timer:
                    run_command(["systemctl", "daemon-reload"])
                    verify_deadlines(run_command, monotonic, before_deadlines)
                    record("ROLLED_BACK_TIMER_PINNED")
                else:
                    record("ROLLED_BACK")
            except BaseException as exc:
                record("ROLLBACK_FAILED")
                raise RuntimeError("종료 설정 복구 확인 실패. 이전 설정: " + str(backup)) from exc
            raise
        return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", help="Reviewed full commit SHA containing these host files")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("sudo python3로 운영 VM 안에서 실행하세요.")
    os.umask(0o077)
    # Catch an interrupted SSH session/TERM so partial writes can be rolled back.
    def interrupted(signum, frame):
        raise KeyboardInterrupt("종료 설정 적용이 중단됐습니다.")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        backup = apply(download(args.revision))
    except (Exception, KeyboardInterrupt) as exc:
        print("종료 설정 적용 실패: " + str(exc))
        return 1
    print("종료 설정 적용 완료: 백업 최대 45분 · 정리 최대 5회/10분 · 종료 전체 최대 70분")
    print("앱·DB는 유지하고 원래 종료 예약을 고정했습니다. 이전 설정: " + str(backup))
    checked = json.loads((backup / "manifest.json").read_text())
    current, elapsed = datetime.now(timezone.utc), time.monotonic_ns() // 1000
    for name, label in (("sessionquant-market-close.timer", "장 마감 예약"),
                        ("sessionquant-cost-guard.timer", "최대 실행 시간 예약")):
        when = current + timedelta(microseconds=checked["timer_deadlines"][name] - elapsed)
        print(label + ": " + when.astimezone(timezone(timedelta(hours=9), "KST")).strftime("%Y-%m-%d %H:%M:%S %Z"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
