"""Private, atomic profile selection and read-only target-ledger inspection."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

CONFIG = 'config/sessionquant.strategy-lab-websocket.example.yaml'
NAMESPACE = 'sessionquant-strategy-lab-ws-v1'
VALUES = {'SQ_SESSION_CONFIG': CONFIG, 'SQ_DB_NAMESPACE': NAMESPACE}


def inspect(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('app.env 실제 파일을 확인하세요.')
    raw = path.read_bytes()
    found = {}
    for line in raw.decode().splitlines():
        match = re.match(r'^\s*(?:export\s+)?(SQ_SESSION_CONFIG|SQ_DB_NAMESPACE)\s*=\s*(.*?)\s*$', line)
        if not match:
            continue
        key, value = match.groups()
        if key in found:
            raise ValueError('프로필 설정이 중복되어 있습니다. 먼저 정리하세요.')
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        found[key] = value
    selected = found.get('SQ_SESSION_CONFIG') == CONFIG
    target_ns = found.get('SQ_DB_NAMESPACE', '').lower() == NAMESPACE
    if selected != target_ns:
        raise ValueError('웹소켓 설정과 장부 이름이 서로 다릅니다. 자동 전환하지 않습니다.')
    return raw, selected, sha256(raw).hexdigest()


def apply(path, backup, expected):
    raw, already, fingerprint = inspect(path)
    if already or fingerprint != expected:
        raise ValueError('점검 이후 설정이 바뀌었거나 이미 전환됐습니다.')
    backup.mkdir(mode=0o700, parents=False, exist_ok=False)
    shutil.copy2(path, backup / 'app.env')
    (backup / 'app.env').chmod(0o600)
    if (backup / 'app.env').read_bytes() != raw:
        raise ValueError('원래 설정 백업 검증 실패')
    lines = [line for line in raw.decode().splitlines()
             if not re.match(r'^\s*(?:export\s+)?(?:SQ_SESSION_CONFIG|SQ_DB_NAMESPACE)\s*=', line)]
    updated = '\n'.join([*lines, *(f'{key}={value}' for key, value in VALUES.items()), '']).encode()
    fd, name = tempfile.mkstemp(prefix='.app.env.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != raw:
            raise ValueError('전환 중 설정 파일이 바뀌었습니다.')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    print('웹소켓 설정·별도 장부 선택 완료. 원래 설정: ' + str(backup / 'app.env'))


def verify_target(runtime, state, control, unbound_records, pending=0):
    if runtime is None and state is None and not unbound_records and not pending:
        return '새 웹소켓 장부로 시작합니다.'
    if (not isinstance(runtime, dict) or runtime.get('version') != 2 or runtime.get('environment') != 'PAPER_LOCAL'
            or runtime.get('source') != 'toss' or runtime.get('identity') != 'toss-readonly-websocket-v1'
            or runtime.get('live_orders_enabled') is not False or runtime.get('synthetic') is not False
            or not isinstance(state, dict) or state.get('schema_version') not in (2, 3)
            or state.get('synthetic') is not False or state.get('environment') != 'PAPER_LOCAL'
            or state.get('strategy') != 'pullback_simple' or state.get('paper_data_policy') != 'observed_paper_v1'):
        raise ValueError('대상 장부에 다른 실행의 기록이 있습니다. 덮어쓰지 않습니다.')
    if (not isinstance(control, dict) or control.get('mode') != 'paper' or control.get('status') != 'OFFLINE'
            or control.get('armed') is not False or control.get('running') is not False or pending):
        raise ValueError('대상 웹소켓 장부의 종료 상태를 확인하지 못했습니다.')
    return '기존 웹소켓 장부를 복구합니다. 원래 보유·주문 기록은 유지됩니다.'


def probe_target():
    import pymysql
    from sessionquant.database.config import MySQLConfig, namespace_for
    db = MySQLConfig.from_env()
    ns = namespace_for('data/sessionquant-strategy-lab-ws-v1.db', NAMESPACE, 'PAPER_LOCAL')
    conn = pymysql.connect(host=db.host, port=db.port, user=db.user, password=db.password,
        database=db.database, charset='utf8mb4', connect_timeout=5, read_timeout=15)
    try:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION READ ONLY')
            def payload(table, name=None):
                sql = f'SELECT payload FROM {table} WHERE namespace=%s'
                args = (ns,)
                if name is not None:
                    sql += ' AND name=%s'
                    args += (name,)
                cur.execute(sql, args)
                row = cur.fetchone()
                return json.loads(row[0]) if row else None
            runtime = payload('pq_controls', 'sq_runtime')
            state = payload('pq_engine_state')
            control = payload('pq_controls', 'trade_control_state')
            unbound = False
            for table in ('pq_controls', 'pq_orders', 'pq_order_intents', 'pq_positions', 'pq_executions'):
                cur.execute(f'SELECT 1 FROM {table} WHERE namespace=%s LIMIT 1', (ns,))
                unbound |= cur.fetchone() is not None
            cur.execute("SELECT COUNT(*) FROM pq_order_intents WHERE namespace=%s "
                        "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.kind'))='dashboard_command' "
                        "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.status'))='pending'", (ns,))
            print(verify_target(runtime, state, control, unbound, cur.fetchone()[0]))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect', 'apply', 'probe-target'])
    parser.add_argument('--env', type=Path, default=Path('/etc/sessionquant/app.env'))
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--expected')
    args = parser.parse_args()
    try:
        if args.action == 'inspect':
            _, already, fingerprint = inspect(args.env)
            print('ALREADY' if already else fingerprint)
        elif args.action == 'apply':
            if args.backup is None or args.expected is None:
                raise ValueError('설정 백업 경로와 점검한 해시가 필요합니다.')
            apply(args.env, args.backup, args.expected)
        else:
            probe_target()
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit('대상 장부 확인 실패. DB 연결·권한을 확인하세요.') from None


if __name__ == '__main__':
    main()
