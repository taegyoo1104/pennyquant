"""Read-only guard for an explicitly paused, flat Paper update during a session."""
import json
import os
import sys
from datetime import datetime, timezone
from hashlib import sha256


def verify_window(now, bounds, *, off_hours=False):
    if off_hours:
        if bounds and now < bounds[1]:
            raise ValueError('휴장일·정규장 마감 후에만 점검 업데이트를 사용하세요.')
    elif not bounds or (bounds[1] - now).total_seconds() < 1800:
        raise ValueError('폐장까지 30분 미만이거나 휴장입니다. 점검 모드는 --off-hours를 사용하세요.')


def verify(control, state, pending, *, stopped=False):
    status_ok = (control.get('status') == 'OFFLINE' and control.get('armed') is False
                 if stopped else control.get('status') in {'WAITING', 'STOPPED'})
    if (control.get('mode') != 'paper' or control.get('running') is not False
            or not status_ok):
        raise ValueError('대시보드에서 매매 종료 후 종료 완료 상태를 확인하세요.')
    if state.get('environment') != 'PAPER_LOCAL' or not isinstance(state.get('broker'), dict):
        raise ValueError('모의매매 장부를 확인하지 못했습니다.')
    broker = state['broker']
    orders = broker.get('orders')
    if (not isinstance(orders, dict) or not isinstance(broker.get('positions'), dict)
            or not isinstance(broker.get('exit_intents'), dict)):
        raise ValueError('주문 장부 형식을 확인하지 못했습니다.')
    if (broker.get('positions') or broker.get('exit_intents') or pending
            or any(row.get('status') not in {'filled', 'cancelled', 'rejected'} for row in orders.values())):
        raise ValueError('보유·미체결·청산 또는 제어 요청이 남아 있어 업데이트를 중단합니다.')


def main():
    import pymysql
    from sessionquant.database.config import MySQLConfig, namespace_for
    from sessionquant.session_config import load_session_config
    from sessionquant.market.bars import session_bounds
    options = set(sys.argv[1:])
    if not options <= {'--stopped', '--off-hours'}:
        raise ValueError('지원하지 않는 업데이트 점검 옵션입니다.')
    off_hours = '--off-hours' in options
    now = datetime.now(timezone.utc)
    bounds = session_bounds(now)
    verify_window(now, bounds, off_hours=off_hours)
    cfg = load_session_config(os.environ.get('SQ_SESSION_CONFIG') or 'config/sessionquant.realtime-paper.example.yaml')
    if cfg.environment != 'PAPER_LOCAL' or cfg.database_backend != 'mysql':
        raise ValueError('모의매매 환경만 업데이트할 수 있습니다.')
    db = MySQLConfig.from_env()
    if db.environment not in {None, 'PAPER_LOCAL'}:
        raise ValueError('DB 실행 환경이 모의매매와 다릅니다.')
    ns = namespace_for(cfg.db_path, db.namespace, 'PAPER_LOCAL')
    conn = pymysql.connect(host=db.host, port=db.port, user=db.user, password=db.password,
                           database=db.database, charset='utf8mb4', connect_timeout=5, read_timeout=10)
    try:
        with conn.cursor() as cur:
            if off_hours:
                name = 'pennyquant:' + sha256(f'{db.database}:{ns}'.encode()).hexdigest()[:48]
                cur.execute('SELECT GET_LOCK(%s,0)', (name,))
                if cur.fetchone() != (1,):
                    raise ValueError('다른 매매 엔진이 실행 중입니다.')
            cur.execute('START TRANSACTION READ ONLY')
            cur.execute('SELECT payload FROM pq_controls WHERE namespace=%s AND name=%s',
                        (ns, 'trade_control_state'))
            row = cur.fetchone()
            control = json.loads(row[0]) if row else {}
            cur.execute('SELECT payload FROM pq_engine_state WHERE namespace=%s', (ns,))
            row = cur.fetchone()
            state = json.loads(row[0]) if row else {}
            cur.execute("SELECT COUNT(*) FROM pq_order_intents WHERE namespace=%s "
                        "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.kind'))='dashboard_command' "
                        "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.status'))='pending' "
                        "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.run_id'))=%s", (ns, control.get('run_id')))
            verify(control, state, cur.fetchone()[0], stopped=off_hours or '--stopped' in options)
    finally:
        conn.close()
    print('모의매매 종료·보유 및 미체결 없음 확인 완료')


if __name__ == '__main__':
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit('장부 조회 실패: DB 연결과 설정을 확인하세요. 업데이트를 중단합니다.') from None
