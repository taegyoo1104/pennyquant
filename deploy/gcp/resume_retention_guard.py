"""Read-only proof for resuming an update interrupted after backup, pre-open."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sys


def validate_receipt(text, now):
    match = re.fullmatch(r"UPLOADED (\S+) (sessionquant-[0-9]{8}T[0-9]{6}Z-[a-zA-Z0-9-]+\.tar\.gz)", text)
    if match is None:
        raise ValueError('성공한 백업 기록이 필요합니다.')
    stamp = datetime.fromisoformat(match[1].replace('Z', '+00:00'))
    if stamp.tzinfo is None or not timedelta(0) <= now - stamp <= timedelta(hours=1):
        raise ValueError('백업 완료 후 1시간이 지났습니다. 일반 업데이트로 새 백업이 필요합니다.')
    return stamp


def validate_control(row, uploaded):
    if not row:
        raise ValueError('매매 종료 기록을 확인할 수 없습니다.')
    payload, updated = row
    payload = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(payload, dict) or (payload.get('mode'), payload.get('status')) != ('paper', 'OFFLINE'):
        raise ValueError('정상 종료된 모의매매만 재개할 수 있습니다.')
    if payload.get('running') is not False or payload.get('armed') is not False:
        raise ValueError('매매가 종료된 상태가 아닙니다.')
    updated = updated.replace(tzinfo=timezone.utc) if updated.tzinfo is None else updated
    if updated > uploaded:
        raise ValueError('백업 이후 실행 상태가 변경됐습니다. 일반 업데이트로 새 백업이 필요합니다.')


def main():
    from sessionquant.build_info import read_build_info
    from sessionquant.database.config import MySQLConfig
    from sessionquant.database.mysql_repository import MySQLRepository
    from sessionquant.session_config import load_session_config
    if len(sys.argv) != 3 or not re.fullmatch('[0-9a-f]{40}', sys.argv[1]):
        raise ValueError('중단된 업데이트의 커밋과 백업 기록이 필요합니다.')
    if read_build_info().get('revision') != sys.argv[1]:
        raise ValueError('현재 코드와 실행 이미지 버전이 다릅니다.')
    uploaded = validate_receipt(sys.argv[2], datetime.now(timezone.utc))
    config = load_session_config(os.environ.get('SQ_SESSION_CONFIG') or 'config/sessionquant.realtime-paper.example.yaml')
    db = MySQLConfig.from_env()
    if config.database_backend != 'mysql' or config.environment != 'PAPER_LOCAL' or db.environment not in {None, 'PAPER_LOCAL'}:
        raise ValueError('MySQL 모의매매만 업데이트를 재개할 수 있습니다.')
    with closing(MySQLRepository(config.db_path, replace(db, environment='PAPER_LOCAL'), initialize=False)) as repo:
        with repo.engine_lease():
            validate_control(repo._execute('SELECT payload,updated_at FROM pq_controls WHERE namespace=%s AND name=%s',
                (repo.namespace, 'trade_control_state'), fetch='one'), uploaded)
    print('최근 백업·프로그램 정상 종료·백업 이후 미실행 확인 완료')


if __name__ == '__main__':
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit('업데이트 재개 조건을 확인하지 못했습니다. 기존 자료를 보존하고 중단합니다.') from None
