"""Defer another deletion pass when the original backup window is too short."""
from datetime import datetime, timezone
from pathlib import Path
import re
import sys


def has_window(current, previous, now):
    match = re.fullmatch(r'UPLOADED (\S+) (sessionquant-[0-9]{8}T[0-9]{6}Z-[a-zA-Z0-9-]+\.tar\.gz)', current)
    if match is None or current == previous:
        raise ValueError('이번 작업의 성공한 백업 기록을 확인할 수 없습니다.')
    stamp = datetime.fromisoformat(match[1].replace('Z', '+00:00'))
    if stamp.tzinfo is None or now < stamp:
        raise ValueError('백업 기록의 시각을 확인할 수 없습니다.')
    # One worker has a 120s timeout plus 15s termination grace. Do not begin
    # another pass that might reach its receipt check after the 1h deadline.
    return (now - stamp).total_seconds() <= 3600 - 150


if __name__ == '__main__':
    try:
        current = Path('/etc/sessionquant/backup-status').read_text().strip()
        enough = has_window(current, sys.argv[1], datetime.now(timezone.utc))
        if not enough:
            print('백업 유효시간이 부족하여 추가 삭제를 보류합니다. 남은 자료는 다음 정리에서 확인합니다.')
        raise SystemExit(0 if enough else 3)
    except (ValueError, OSError, IndexError) as exc:
        raise SystemExit(str(exc)) from None
