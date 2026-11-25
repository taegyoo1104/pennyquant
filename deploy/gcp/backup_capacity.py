"""Bounded local capacity evidence; never query MySQL or remove a backup."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

GIB = 1024 ** 3
RESERVE = 2 * GIB
CHUNK = 8 * 1024 ** 2


def file_bytes(root, largest=None):
    if not root.is_dir():
        raise OSError('자료 디렉터리 확인 필요')
    total, entries, started = 0, 0, time.monotonic()
    def unreadable(error):
        raise error
    # Metadata only, no table reads and no traversal through symlinks.
    for directory, directories, names in os.walk(root, onerror=unreadable):
        entries += len(names) + len(directories)
        if entries > 20000 or time.monotonic() - started > 5:
            raise TimeoutError('파일 크기 점검 한도 초과')
        for name in names:
            info = (Path(directory) / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if largest is not None:
                    largest.append({'file': str((Path(directory)/name).relative_to(root)), 'bytes': info.st_size})
                    largest.sort(key=lambda row: row['bytes'], reverse=True)
                    del largest[7:]
    return total


def atomic(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write('\n')
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read(path):
    try:
        if path.stat().st_size > 16384:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def positive(value):
    return type(value) is int and value > 0


def estimate(database_bytes, archive_bytes, observation):
    compressed = observation.get('compressed_bytes')
    measured_database = observation.get('database_bytes')
    if positive(compressed) and positive(measured_database):
        # Allocated size cannot prove logical size. Do not shrink the estimate
        # merely because files became smaller; keep headroom and a live guard.
        baseline = max(compressed, math.ceil(compressed * database_bytes / measured_database))
        basis = 'MEASURED_DUMP_WITH_GROWTH'
    else:
        # Bootstrap heuristic only, not a promised compression ratio.
        baseline = max(GIB, database_bytes // 4, archive_bytes * 2)
        basis = 'BOOTSTRAP_ESTIMATE'
    return math.ceil(max(baseline, archive_bytes) * 1.25), basis


def free_bytes(path):
    info = os.statvfs(path)
    if info.f_files and info.f_favail / info.f_files < .05:
        raise OSError('파일 수 여유 부족')
    return info.f_bavail * info.f_frsize


def status_line(path):
    try:
        return path.read_text()[:1024].strip()
    except OSError:
        return 'MISSING'


def measure(data, etc):
    largest = []
    database = file_bytes(data / 'mysql', largest)
    app = file_bytes(data / 'app')
    archive = max((p.stat().st_size for p in (data / 'backups').glob('sessionquant-*.tar.gz')
                   if p.is_file() and not p.is_symlink()), default=0)
    predicted, basis = estimate(database, archive, read(etc / 'backup-capacity.json'))
    # Include two SQL copies, app/tar overhead, and room for the live database.
    app_allowance = app * 2 + 64 * 1024 ** 2
    required = predicted * 2 + app_allowance + RESERVE
    free = free_bytes(data / 'backups')
    return {'version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'database_file_bytes': database, 'largest_database_files': largest, 'app_bytes': app,
            'estimated_compressed_bytes': predicted, 'estimate_basis': basis,
            'required_free_bytes': required, 'free_bytes': free,
            'status': 'LOW' if free < required else 'WARNING' if free < required + 4 * GIB else 'OK',
            'backup_status': status_line(etc / 'backup-status'),
            'retention_status': status_line(etc / 'retention-status')}


def publish(data, report):
    # Separate from /app/data: monitoring must not invalidate the sealed backup.
    health = data / 'health'
    health.mkdir(parents=True, exist_ok=True)
    os.chmod(health, 0o755)
    atomic(health / 'storage.json', report, 0o644)


def require_space(path, required):
    free = free_bytes(path)
    if free < required:
        raise OSError(f'백업 공간 부족: 여유 {free / GIB:.1f} GiB / 필요 {required / GIB:.1f} GiB')


def write_dump(source, destination, app_allowance):
    # The compressed dump and its future archive must both fit. Enforce this
    # even when today's logical growth exceeds the historical estimate.
    size = 0
    with destination.open('xb') as output:
        while chunk := source.read(CHUNK):
            require_space(destination.parent, size + 2 * len(chunk) + app_allowance + RESERVE)
            output.write(chunk)
            size += len(chunk)
    return size


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['check', 'report', 'dump', 'package'])
    parser.add_argument('path', nargs='?', type=Path)
    parser.add_argument('--data', type=Path, default=Path('/var/lib/sessionquant'))
    parser.add_argument('--etc', type=Path, default=Path('/etc/sessionquant'))
    args = parser.parse_args()
    try:
        if args.action in {'check', 'report'}:
            report = measure(args.data, args.etc)
            publish(args.data, report)
            print(f"백업 공간 예상: 여유 {report['free_bytes']/GIB:.1f} GiB / "
                  f"필요 {report['required_free_bytes']/GIB:.1f} GiB · {report['status']} (추정)")
            return int(args.action == 'check' and report['status'] == 'LOW')
        if args.path is None:
            parser.error('dump/package에는 파일 경로가 필요합니다.')
        allowance = file_bytes(args.data / 'app') * 2 + 64 * 1024 ** 2
        if args.action == 'dump':
            write_dump(sys.stdin.buffer, args.path, allowance)
        else:
            size = args.path.stat().st_size
            atomic(args.etc / 'backup-capacity.json', {
                'version': 1, 'measured_at': datetime.now(timezone.utc).isoformat(),
                'compressed_bytes': size, 'database_bytes': file_bytes(args.data / 'mysql')})
            require_space(args.path.parent, math.ceil(size * 1.01) + allowance + RESERVE)
        return 0
    except (OSError, ValueError) as error:
        publish(args.data, {'version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
                           'status': 'UNKNOWN', 'error': type(error).__name__,
                           'backup_status': status_line(args.etc / 'backup-status'),
                           'retention_status': status_line(args.etc / 'retention-status')})
        print(f'백업 공간 확인 실패: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
