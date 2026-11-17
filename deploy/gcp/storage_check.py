"""Check actual DB/backup filesystems; thresholds do not guarantee an unlimited dump fits."""
import argparse
import os
from pathlib import Path


def check(paths, minimum_mib):
    healthy = True
    for path in paths:
        try:
            info = os.statvfs(path)
            free = info.f_bavail * info.f_frsize
            inode_ok = not info.f_files or info.f_favail / info.f_files >= 0.05
            ok = free >= minimum_mib * 1024 * 1024 and inode_ok
            print(f'{path}: 여유 {free // (1024 * 1024)} MiB / 필요 {minimum_mib} MiB · '
                  f'{"정상" if ok else "저장 공간 부족"}')
            healthy = healthy and ok
        except OSError:
            print(f'{path}: 저장 공간을 확인할 수 없습니다.')
            healthy = False
    return healthy


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--minimum-mib', type=int, default=2048)
    parser.add_argument('paths', nargs='*', type=Path,
                        default=[Path('/var/lib/sessionquant/mysql'), Path('/var/lib/sessionquant/backups')])
    args = parser.parse_args()
    if args.minimum_mib <= 0:
        parser.error('minimum-mib must be positive')
    raise SystemExit(0 if check(args.paths, args.minimum_mib) else 1)
