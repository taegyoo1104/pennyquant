"""Explicit off-hours projection preparation; no source edits or deletion."""
import argparse
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from time import monotonic

from update_guard import verify, verify_window

CURSOR = 'db_projection_preparation_v1'


class PreparationBlocked(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def valid_cursor(saved, namespace, signature):
    return (isinstance(saved, dict) and type(saved.get('version')) is int and saved['version'] == 1
            and saved.get('namespace') == namespace and saved.get('trigger_signature') == signature
            and type(saved.get('after')) is int and saved['after'] >= 0
            and saved.get('phase') in {'BACKFILL', 'VERIFY'})


def prepare(repo, *, seconds=60, restart=False, now=None, clock=monotonic, bounds=None):
    from sessionquant.database import llm_attempt_index as llm, pipeline_summary as pipeline
    from sessionquant.market.bars import session_bounds
    if type(seconds) is not int or not 1 <= seconds <= 300:
        raise ValueError('작업 시간은 1~300초입니다.')
    if repo.backend != 'mysql' or repo.config.environment != 'PAPER_LOCAL' or repo._in_transaction:
        raise ValueError('MySQL PAPER_LOCAL의 종료된 장부에서만 실행하세요.')
    now = now or datetime.now(timezone.utc)
    verify_window(now, session_bounds(now) if bounds is None else bounds, off_hours=True)
    started = clock()
    result = {'version': 1, 'namespace': repo.namespace, 'checked_at': now.isoformat(),
              'status': 'PENDING', 'reason': 'TIME_LIMIT', 'source_rows_changed': 0,
              'pipeline': {'checked': 0, 'backfilled': 0, 'complete': False},
              'llm': {'backfilled': 0, 'complete': False, 'reused_proof': False}}
    deadline = started + seconds
    phase = 'GUARD'
    try:
        repo._execute('SET SESSION innodb_lock_wait_timeout=3')
        repo._execute('SET SESSION lock_wait_timeout=3')
        repo._execute('SET SESSION MAX_EXECUTION_TIME=3000')
        with repo.engine_lease():
            readonly = repo._execute('SELECT @@GLOBAL.read_only,@@GLOBAL.super_read_only', fetch='one')
            if not readonly or any(readonly):
                raise PreparationBlocked('DB_READ_ONLY')
            control = repo.get_control('trade_control_state', {}) or {}
            state = repo.load_state() or {}
            pending = repo._execute('SELECT COUNT(*) FROM pq_order_intents WHERE namespace=%s '
                "AND payload->>'$.kind'='dashboard_command' AND payload->>'$.status'='pending' "
                "AND payload->>'$.run_id'=%s", (repo.namespace, control.get('run_id')), fetch='one')
            try:
                verify(control, state, pending[0], stopped=True)
            except ValueError:
                raise PreparationBlocked('TRADING_STATE_NOT_READY') from None
            if clock() >= deadline:
                return result
            phase = 'INSTALL'
            pipeline.install(repo)
            llm.install(repo)
            signature = llm.trigger_signature(repo)
            if not pipeline.available(repo) or not signature:
                raise PreparationBlocked('SCHEMA_UNAVAILABLE')
            saved = repo.get_control(CURSOR)
            cursor = saved if not restart and valid_cursor(saved, repo.namespace, signature) else {
                'version': 1, 'namespace': repo.namespace, 'trigger_signature': signature,
                'after': 0, 'phase': 'BACKFILL'}
            # An explicit restart invalidates the prior proof/cursor before
            # any pipeline work that could exhaust the budget or fail.
            if restart or not valid_cursor(saved, repo.namespace, signature):
                with repo.transaction():
                    if restart:
                        llm.invalidate(repo)
                    repo.set_control(CURSOR, cursor)
            phase = 'PIPELINE'
            keys = repo._execute('SELECT seq FROM pq_pipeline_status FORCE INDEX (ix_namespace_seq) '
                'WHERE namespace=%s ORDER BY seq DESC LIMIT 100', (repo.namespace,), fetch='all')
            for start in range(0, len(keys), 10):
                if clock() >= deadline:
                    return result
                ids = [row[0] for row in keys[start:start + 10]]
                result['pipeline']['backfilled'] += pipeline.backfill_page(repo, ids)
                result['pipeline']['checked'] += len(ids)
            result['pipeline']['complete'] = True
            phase = 'LLM'
            proof = repo.get_control(llm.CONTROL)
            if not restart and llm.covered(proof, repo.namespace, signature):
                result['llm'].update(complete=True, reused_proof=True)
            else:
                llm.invalidate(repo)
                while cursor['phase'] == 'BACKFILL' and clock() < deadline:
                    page = llm.backfill_page(repo, after=cursor['after'], limit=200)
                    result['llm']['backfilled'] += page['rows']
                    cursor = {**cursor, 'after': page['after'], 'phase': 'VERIFY' if page['done'] else 'BACKFILL'}
                    repo.set_control(CURSOR, cursor)
                result['llm'].update(after=cursor['after'], phase=cursor['phase'])
                remaining = int(deadline - clock())
                if cursor['phase'] == 'VERIFY' and remaining > 0:
                    result['llm']['complete'] = llm.prove_coverage(repo, max_seconds=remaining)
                    if not result['llm']['complete']:
                        result['reason'] = 'PROOF_INCOMPLETE'
            if result['pipeline']['complete'] and result['llm']['complete']:
                result.update(status='COMPLETE', reason='PROJECTIONS_READY')
    except Exception as exc:
        code = getattr(exc, 'code', None)
        # Budget/lock timeouts are resumable only after all trading guards pass.
        pending = phase in {'PIPELINE', 'LLM'} and code in {1205, 3024}
        result.update(status='PENDING' if pending else 'FAILED',
                      reason='QUERY_LIMIT' if pending else getattr(exc, 'reason', 'PREPARATION_FAILED'),
                      phase=phase, error={'type': type(exc).__name__, 'code': code})
    finally:
        result['elapsed_seconds'] = round(max(0.0, clock() - started), 3)
    return result


def main():
    from sessionquant.database.config import MySQLConfig
    from sessionquant.database.mysql_repository import MySQLRepository
    from sessionquant.session_config import load_session_config
    parser = argparse.ArgumentParser(description='휴장·폐장 후 조회 요약 준비 (원본 삭제 없음)')
    parser.add_argument('--apply', action='store_true', required=True)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--restart', action='store_true')
    args = parser.parse_args()
    cfg = load_session_config(os.environ.get('SQ_SESSION_CONFIG') or 'config/sessionquant.realtime-paper.example.yaml')
    db = MySQLConfig.from_env()
    if cfg.environment != 'PAPER_LOCAL' or cfg.database_backend != 'mysql' or db.environment not in {None, 'PAPER_LOCAL'}:
        raise ValueError('MySQL PAPER_LOCAL 설정만 지원합니다.')
    with closing(MySQLRepository(cfg.db_path, replace(db, environment='PAPER_LOCAL',
                 read_timeout=5, write_timeout=5, connect_timeout=5), initialize=False)) as repo:
        result = prepare(repo, seconds=args.seconds, restart=args.restart)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result['status'] == 'FAILED' else 0


if __name__ == '__main__':
    try:
        code = main()
    except Exception as exc:
        print(json.dumps({'status': 'FAILED', 'reason': 'COMMAND_SETUP_FAILED',
            'error': {'type': type(exc).__name__, 'code': getattr(exc, 'code', None)}}))
        code = 1
    raise SystemExit(code)
