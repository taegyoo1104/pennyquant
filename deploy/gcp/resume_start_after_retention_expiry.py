"""One-time read-only recovery for PR68's expired second cleanup pass.

Does not authorize deletion or refresh a backup receipt. Verifies the last
successful partial pass and unchanged OFFLINE Paper state, then hands off a
PENDING startup check to the existing normal service (manual trading start).
"""
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

REVISION = '523cd84acaa30750a8adaeca8b53e6390dc223f5'
ROOT = Path('/opt/sessionquant')
ETC = Path('/etc/sessionquant')
REPORT = Path('/var/lib/sessionquant/app/retention-last.json')
SUMMARY = Path('/var/lib/sessionquant/health/startup-retention-last.json')
MARKER = Path('/run/sessionquant-startup-prepared.json')
COMPOSE = ['docker', 'compose', '-f', 'deploy/gcp/compose.yaml']


def stamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('시간대 없는 시각은 복구 근거로 사용할 수 없습니다.')
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_evidence(receipt, report, failure, now):
    match = re.fullmatch(r'UPLOADED (\S+) (sessionquant-[0-9]{8}T[0-9]{6}Z-[a-zA-Z0-9-]+\.tar\.gz)', receipt)
    if match is None:
        raise ValueError('성공한 백업 기록이 필요합니다.')
    uploaded = stamp(match[1])
    if not timedelta(hours=1) <= now - uploaded <= timedelta(hours=6):
        raise ValueError('이 복구는 백업 만료 직후의 중단에만 사용합니다.')
    if (report.get('dry_run') is not False or report.get('status') != 'PARTIAL'
            or report.get('errors') != [] or report.get('backup') != match[2]
            or any(row.get('error') for row in report.get('tables', {}).values())):
        raise ValueError('같은 백업으로 오류 없이 끝난 부분 정리 기록이 필요합니다.')
    started, finished = stamp(report['started_at']), stamp(report['finished_at'])
    if not (uploaded <= started <= uploaded + timedelta(hours=1)
            and started <= finished <= started + timedelta(minutes=3) and finished <= now):
        raise ValueError('백업 유효기간 안에 시작한 정리 기록이 아닙니다.')
    failed = stamp(failure['checked_at'])
    if (failure.get('status') != 'FAILED' or failure.get('reason') != 'CLEANUP_FAILED'
            or failure.get('cleanup_attempted') is not True
            or not max(finished, uploaded + timedelta(hours=1)) <= failed <= finished + timedelta(minutes=5)
            or failed > now):
        raise ValueError('부분 정리 후 백업 만료로 중단된 시작 기록이 아닙니다.')
    return uploaded


def validate_control(row, uploaded):
    if not row:
        raise ValueError('매매 종료 기록이 없습니다.')
    payload, updated = row
    payload = json.loads(payload) if isinstance(payload, str) else payload
    if (payload.get('mode') != 'paper' or payload.get('status') != 'OFFLINE'
            or payload.get('running') is not False or payload.get('armed') is not False):
        raise ValueError('정상 종료된 모의매매 상태가 아닙니다.')
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    if updated > uploaded:
        raise ValueError('백업 이후 매매 실행 상태가 변경됐습니다.')


def verify_db(receipt, expected_report):
    from contextlib import closing
    from dataclasses import replace
    from sessionquant.build_info import read_build_info
    from sessionquant.database.config import MySQLConfig
    from sessionquant.database.mysql_repository import MySQLRepository
    from sessionquant.session_config import load_session_config
    if read_build_info().get('revision') != REVISION:
        raise ValueError('검증한 실행 이미지가 아닙니다.')
    config = load_session_config(os.environ.get('SQ_SESSION_CONFIG') or 'config/sessionquant.realtime-paper.example.yaml')
    db = MySQLConfig.from_env()
    if config.database_backend != 'mysql' or config.environment != 'PAPER_LOCAL' or db.environment not in {None, 'PAPER_LOCAL'}:
        raise ValueError('MySQL 모의매매만 복구합니다.')
    uploaded = stamp(receipt.split()[1])
    with closing(MySQLRepository(config.db_path, replace(db, environment='PAPER_LOCAL'), initialize=False)) as repo:
        with repo.engine_lease():
            report = repo.get_control('data_retention_v1', {})
            if report.get('namespace') != repo.namespace or digest(report) != expected_report:
                raise ValueError('파일과 DB의 마지막 정리 기록이 다릅니다.')
            validate_control(repo._execute(
                'SELECT /*+ MAX_EXECUTION_TIME(3000) */ payload,updated_at FROM pq_controls WHERE namespace=%s AND name=%s',
                (repo.namespace, 'trade_control_state'), fetch='one'), uploaded)
    print('마지막 부분 정리·백업 이후 미실행 확인 완료')


def run(args, *, capture=False, input=None, timeout=60):
    result = subprocess.run(args, cwd=ROOT, check=True, text=True,
                            input=input, stdin=subprocess.DEVNULL if input is None else None,
                            stdout=subprocess.PIPE if capture else None, timeout=timeout)
    return result.stdout.strip() if capture else None


def lock(fd, name):
    opened = os.open('/run/' + name, os.O_RDWR | os.O_CREAT, 0o600)
    if opened != fd:
        os.dup2(opened, fd)
        os.close(opened)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def recover():
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != 'sessionquant-paper':
        raise ValueError('sessionquant-paper에서 sudo로 실행하세요.')
    lock(9, 'sessionquant-update.lock')
    lock(8, 'sessionquant-lifecycle.lock')
    if any((ETC / name).exists() for name in ('maintenance', 'storage-blocked')):
        raise ValueError('기존 점검·저장 공간 차단을 먼저 확인하세요.')
    for unit in ('sessionquant', 'sessionquant-market-close', 'sessionquant-run-limit', 'sessionquant-cost-guard'):
        state = run(['systemctl', 'show', unit + '.service', '-p', 'ActiveState', '--value'], capture=True)
        if state not in {'inactive', 'failed'}:
            raise ValueError('실행 중인 서비스가 있습니다: ' + unit)
    if run(COMPOSE + ['ps', '--status', 'running', '--services', 'app'], capture=True):
        raise ValueError('대시보드가 실행 중입니다. 종료 상태를 확인하세요.')
    if run(['git', 'rev-parse', 'HEAD'], capture=True) != REVISION:
        raise ValueError('PR68을 적용한 중단 상태에서만 복구합니다.')
    receipt = (ETC / 'backup-status').read_text().strip()
    report = json.loads(REPORT.read_text())
    previous = json.loads(SUMMARY.read_text())
    validate_evidence(receipt, report, previous, datetime.now(timezone.utc))
    if not (ETC / 'retention-status').read_text().startswith('FAILED '):
        raise ValueError('정리 중단 상태가 아닙니다.')
    proof = ['python', '-u', '-', '--verify', receipt, digest(report)]
    source = Path(__file__).read_text()
    run(COMPOSE + ['run', '--rm', '--no-deps', '-T', 'app', *proof], input=source, timeout=90)
    # Only the existing pre-open recovery is covered; never race into a session.
    calendar = '''from datetime import datetime,timezone
from sessionquant.market.bars import session_bounds
now=datetime.now(timezone.utc); bounds=session_bounds(now)
if not bounds or now>=bounds[0]: raise SystemExit("장전 복구만 지원합니다.")
'''
    run(['docker', 'run', '--rm', '--network', 'none', 'sessionquant:local', 'python', '-c', calendar])
    run(['bash', 'deploy/gcp/preopen_check.sh'], timeout=180)
    spec = importlib.util.spec_from_file_location('startup_retention', ROOT / 'deploy/gcp/startup_retention.py')
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    after, reason = startup.probe()
    if after not in {'CLEAR', 'NEEDS_CLEANUP', 'DISABLED'}:
        raise ValueError('현재 DB의 정리 대상 확인에 실패했습니다. 시작하지 않습니다.')
    if ((ETC / 'backup-status').read_text().strip() != receipt or digest(json.loads(REPORT.read_text())) != digest(report)):
        raise ValueError('복구 확인 중 백업·정리 기록이 변경됐습니다.')
    run(COMPOSE + ['run', '--rm', '--no-deps', '-T', 'app', *proof], input=source, timeout=90)
    summary = {'version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
               'before': after, 'after': after, 'cleanup_attempted': False, 'backup_reused': True,
               'status': 'PENDING' if after == 'NEEDS_CLEANUP' else after,
               'reason': 'RECOVERED_AFTER_BACKUP_EXPIRY', 'probe_reason': reason,
               'previous_failure': previous, 'verified_retention_digest': digest(report)}
    # Preserve the failure in the new summary. Never rewrite the actual backup
    # timestamp, retention report, cursors or financial records.
    startup.persist(summary)
    try:
        run(['python3', 'deploy/gcp/startup_handoff.py', 'create'])
        fcntl.flock(8, fcntl.LOCK_UN)
        os.close(8)
        run(['systemctl', 'reset-failed', 'sessionquant.service'])
        run(['systemctl', 'start', 'sessionquant.service'], timeout=900)
    finally:
        MARKER.unlink(missing_ok=True)
    for timer in ('storage-guard', 'market-close', 'run-limit'):
        run(['systemctl', 'is-active', '--quiet', 'sessionquant-' + timer + '.timer'])
    run(COMPOSE + ['ps'])
    print('정상 프로그램 시작 완료. 남은 자료 정리는 보류했습니다. 자료 준비 후 매매 시작을 직접 누르세요.')


if __name__ == '__main__':
    try:
        if len(sys.argv) == 4 and sys.argv[1] == '--verify':
            verify_db(sys.argv[2], sys.argv[3])
        elif len(sys.argv) == 1:
            recover()
        else:
            raise ValueError('알 수 없는 복구 인자입니다.')
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit('복구 중단: ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)) from None
