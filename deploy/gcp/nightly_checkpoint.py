"""Reuse only an unchanged, read-only MySQL snapshot backed up after cleanup.

Host/root helper. Never trusts table row estimates or the absence of trades.
A DB restart, SQL write, app-file change, missing archive or changed receipt
invalidates reuse. Normal runtime must invalidate the proof before unsealing.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path('/opt/sessionquant')
ETC = Path('/etc/sessionquant')
DATA = Path('/var/lib/sessionquant')
PROOF = ETC / 'nightly-checkpoint.json'
SEAL = ETC / 'nightly-seal.json'
COMPOSE = ['docker', 'compose', '-f', str(ROOT / 'deploy/gcp/compose.yaml')]
WRITE = re.compile(r'Com_(insert|update|delete|replace|load|truncate|create|alter|drop|rename|grant|revoke|install|uninstall|reset|purge|repair|optimize|analyze|flush)(_|$)', re.I)


def run(args, *, input=None):
    return subprocess.run(args, input=input, text=True, capture_output=True,
                          check=True, timeout=30).stdout.strip()


def sql(statement):
    # Password stays inside the container, never in argv/output or a receipt.
    return run([*COMPOSE, 'exec', '-T', 'db', 'sh', '-c',
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B'], input=statement+';\n')


def atomic(path, data):
    part = path.with_suffix('.tmp')
    with part.open('w', encoding='utf-8') as stream:
        os.chmod(part, 0o600)
        json.dump(data, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    part.replace(path)


def stopped():
    if run([*COMPOSE, 'ps', '--status', 'running', '--services', 'app']):
        raise ValueError('프로그램을 종료한 뒤 백업 재사용을 확인하세요.')


def process_identity():
    # Linux process start ticks and the kernel boot ID do not use wall time.
    # The supported mysql:8.4 image execs mysqld as PID 1. Unknown layouts fail
    # closed rather than treating a shell/supervisor as the database process.
    lines = run(['docker', 'exec', 'sessionquant-db-1', 'cat',
                 '/proc/1/stat', '/proc/1/comm', '/proc/sys/kernel/random/boot_id']).splitlines()
    if len(lines) != 3 or lines[1].strip() != 'mysqld':
        raise ValueError('DB 프로세스 시작 정보 확인 실패')
    stat, _, boot = lines
    fields = stat.rsplit(')', 1)[-1].split()
    if (not stat.startswith('1 (mysqld) ') or len(fields) < 20
            or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', boot)):
        raise ValueError('DB 프로세스 시작 정보 형식 확인 실패')
    ticks = int(fields[19])  # proc(5): field 22; suffix starts at field 3.
    if ticks <= 0:
        raise ValueError('DB 프로세스 시작 시각 확인 실패')
    return {'boot_id': boot, 'start_ticks': ticks}


def snapshot():
    identity = json.loads(run(['docker', 'inspect', 'sessionquant-db-1', '--format',
                              '{{json .State}}']))
    if identity.get('Status') != 'running':
        raise ValueError('DB 실행 상태 확인 실패')
    process = process_identity()
    server, readonly = sql('SELECT @@server_uuid, @@GLOBAL.super_read_only').split('\t')
    stats = dict(line.split('\t', 1) for line in sql('SHOW GLOBAL STATUS').splitlines())
    writes = {key: int(value) for key, value in stats.items()
              if WRITE.match(key) or key in {'Innodb_rows_inserted', 'Innodb_rows_updated', 'Innodb_rows_deleted'}}
    if not {'Com_insert', 'Com_update', 'Com_delete', 'Com_truncate', 'Com_flush'} <= writes.keys():
        raise ValueError('DB 변경 횟수 확인 실패')
    uptime, flush = int(stats['Uptime']), int(stats['Uptime_since_flush_status'])
    if uptime < 0 or not 0 <= flush <= uptime or process_identity() != process:
        raise ValueError('DB 상태 확인 도중 재시작 또는 상태 초기화가 발생했습니다.')
    return {'clock_version': 2, 'container_started': identity['StartedAt'], 'server_uuid': server,
            'readonly': readonly == '1', 'writes': writes,
            'process': process, 'flush_offset': uptime-flush}


def differences(before, after):
    # Old wall-clock-only seals cannot be upgraded retrospectively: they did
    # not capture a process identity before the backup. Require a new seal.
    if before.get('clock_version') != 2 or after.get('clock_version') != 2:
        return ['비교 형식 변경: 새 백업 필요']
    changed = []
    if before.get('readonly') is not True or after.get('readonly') is not True:
        changed.append('DB 쓰기 금지 상태')
    for key, label in (('container_started', 'DB 컨테이너 시작'), ('server_uuid', 'DB 서버 식별자'),
                       ('writes', 'DB 변경 횟수'), ('process', 'DB 프로세스·시스템 부팅')):
        if key not in before or key not in after or before[key] != after[key]:
            changed.append(label)
    offsets = [value.get('flush_offset') for value in (before, after)]
    if any(type(value) is not int or value < 0 for value in offsets) or abs(offsets[0]-offsets[1]) > 2:
        changed.append('DB 상태 초기화 시점')
    return changed


def unchanged(before, after):
    return not differences(before, after)


def files():
    paths = [ETC/'app.env', ETC/'db.env', ETC/'backup-bucket']
    app = DATA/'app'
    if not app.is_dir():
        raise ValueError('앱 저장 폴더 확인 실패')
    paths += sorted(app.rglob('*'))
    result = {}
    total = 0
    for path in paths:
        if path.is_symlink():
            raise ValueError('백업 대상의 심볼릭 링크 확인 필요')
        if path.is_dir():
            continue
        size = path.stat().st_size
        total += size
        if total > 256*1024*1024:
            raise ValueError('앱 파일 확인 예산 초과: 새 백업 필요')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024*1024):
                digest.update(chunk)
        result[str(path)] = digest.hexdigest()
    return result


def receipt():
    value = (ETC/'backup-status').read_text().strip()
    match = re.fullmatch(r'UPLOADED (\S+) (sessionquant-[0-9]{8}T[0-9]{6}Z-[a-zA-Z0-9-]+\.tar\.gz)', value)
    if not match:
        raise ValueError('완료된 백업 기록 없음')
    stamp = datetime.fromisoformat(match[1].replace('Z', '+00:00'))
    if stamp.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-stamp).total_seconds() <= 96*3600:
        raise ValueError('야간 백업 재사용 기한 96시간 초과')
    archive = DATA/'backups'/match[2]
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size <= 0:
        raise ValueError('야간 백업 파일 확인 실패')
    stat = archive.stat()
    return {'receipt': value, 'archive': [stat.st_size, stat.st_mtime_ns, stat.st_ino]}


def seal():
    stopped()
    PROOF.unlink(missing_ok=True)
    # Remember ownership before SET, including a timed-out client/partial seal.
    if sql('SELECT @@GLOBAL.super_read_only') == '1' and not SEAL.exists():
        raise ValueError('다른 작업의 DB 쓰기 금지 상태입니다.')
    atomic(SEAL, {'version': 1})
    sql('SET GLOBAL super_read_only=ON')
    atomic(SEAL, {'version': 1, 'database': snapshot(), 'files': files(),
                  'backup_before': (ETC/'backup-status').read_text().strip()})


def writable():
    PROOF.unlink(missing_ok=True)
    if SEAL.exists():
        sql('SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF')
        SEAL.unlink()


def record():
    stopped()
    sealed = json.loads(SEAL.read_text())
    before = sealed['database']
    current_files = files()
    changed = differences(before, snapshot())
    if sealed['files'] != current_files:
        changed.append('앱·설정 파일')
    if changed:
        raise ValueError('최종 백업 도중 자료가 변경됐습니다. 확인 항목: ' + ', '.join(changed))
    report = json.loads((DATA/'app/retention-last.json').read_text())
    if (report.get('dry_run') is not False or report.get('status') not in {'COMPLETE', 'DISABLED'}
            or report.get('errors') or report.get('observability', {}).get('backlog_status') == 'PRESENT'):
        raise ValueError('야간 자료 정리 완료를 확인하지 못했습니다.')
    backup = receipt()
    if backup['receipt'] == sealed['backup_before']:
        raise ValueError('정리 후 새 최종 백업을 확인하지 못했습니다.')
    atomic(PROOF, {'version': 1, 'database': snapshot(), 'files': current_files,
                   'backup': backup, 'completed_at': datetime.now(timezone.utc).isoformat()})


def verify():
    stopped()
    proof = json.loads(PROOF.read_text())
    if proof.get('version') != 1 or not SEAL.exists():
        raise ValueError('야간 완료 증명 없음')
    if proof['backup'] != receipt() or proof['files'] != files() or not unchanged(proof['database'], snapshot()):
        raise ValueError('야간 백업 이후 데이터·DB 상태가 변경됐습니다.')


def main():
    if os.geteuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in {'seal', 'record', 'verify', 'writable'}:
        raise ValueError('관리자 야간 백업 명령이 필요합니다.')
    globals()[sys.argv[1]]()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc) if isinstance(exc, ValueError) else '야간 완료 기록을 확인할 수 없습니다. 새 백업이 필요합니다.', file=sys.stderr)
        raise SystemExit(1)
