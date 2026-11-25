"""One-use handoff from a still-running updater to systemd start.

This is not a retention completion or backup-reuse proof. It only prevents a
second cleanup attempt in the same update/start operation, including PARTIAL.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

ROOT = Path('/opt/sessionquant')
ETC = Path('/etc/sessionquant')
DATA = Path('/var/lib/sessionquant/health')
APP_DATA = Path('/var/lib/sessionquant/app')
MARKER = Path('/run/sessionquant-startup-prepared.json')
UPDATE_LOCK = Path('/run/sessionquant-update.lock')


def process_identity(pid):
    # Fields after the parenthesized comm start at field 3; starttime is 22.
    value = Path(f'/proc/{pid}/stat').read_text()
    return value[value.rindex(')') + 2:].split()[19]


def fingerprint():
    paths = [ETC / 'app.env', ETC / 'db.env', ETC / 'backup-status',
             DATA / 'startup-retention-last.json']
    # Configuration may also be selected from the writable /app/data mount.
    # Bound this check and refuse symlinks rather than follow files elsewhere.
    paths += sorted(APP_DATA.rglob('*'))
    values = {}
    total = 0
    for path in paths:
        if path.is_symlink():
            raise ValueError('시작 확인 파일의 심볼릭 링크를 사용할 수 없습니다.')
        if path.is_dir():
            continue
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('일반 시작 확인 파일만 사용할 수 있습니다.')
        total += info.st_size
        if total > 256 * 1024 * 1024:
            raise ValueError('시작 확인 파일의 크기 한도를 넘었습니다.')
        values[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    values['revision'] = subprocess.check_output(
        ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True, timeout=10).strip()
    values['image'] = subprocess.check_output(
        ['docker', 'image', 'inspect', 'sessionquant:local', '--format', '{{.Id}}'],
        text=True, timeout=10).strip()
    return values


def updater_holds_lock(pid):
    fd = Path(f'/proc/{pid}/fd/9')
    if fd.resolve(strict=True) != UPDATE_LOCK:
        return False
    with UPDATE_LOCK.open('r') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def create():
    pid = os.getppid()
    if not updater_holds_lock(pid):
        raise ValueError('업데이트 잠금을 확인할 수 없습니다.')
    report = json.loads((DATA / 'startup-retention-last.json').read_text())
    if report.get('status') not in {'CLEAR', 'PENDING', 'UNKNOWN', 'DISABLED'}:
        raise ValueError('이번 시작 전 정리 확인이 완료되지 않았습니다.')
    payload = {'pid': pid, 'process': process_identity(pid),
               'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
               'at': time.monotonic(), 'fingerprint': fingerprint(), 'status': report['status']}
    MARKER.unlink(missing_ok=True)
    fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(payload, stream)


def consume():
    try:
        info = MARKER.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            return False
        payload = json.loads(MARKER.read_text())
        # Consume even an expired/invalid marker; it cannot authorize a later start.
        MARKER.unlink()
        if not (0 <= time.monotonic() - payload['at'] <= 120
                and payload['boot'] == Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                and payload['process'] == process_identity(payload['pid'])
                and updater_holds_lock(payload['pid'])
                and payload['fingerprint'] == fingerprint()):
            return False
        print('이번 업데이트의 시작 전 정리 확인을 사용합니다: ' + payload['status'])
        return True
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False


def main():
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise SystemExit('관리자 시작 확인 명령이 필요합니다.')
    if sys.argv[1] == 'create':
        create()
    elif sys.argv[1] == 'consume':
        raise SystemExit(0 if consume() else 1)
    else:
        raise SystemExit('알 수 없는 시작 확인 명령')


if __name__ == '__main__':
    main()
