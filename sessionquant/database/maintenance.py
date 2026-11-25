"""Continuous stopped-runtime retention; a backup grant never survives its lease.

The ordinary one-hour receipt rule is unchanged. A maintenance run verifies it
once, freezes its cutoff, and holds the same MySQL engine lease/connection until
completion or its finite deadline. The in-memory grant cannot be resumed from a
file, reused by a new process, or carried over a connection/server restart.
"""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from time import monotonic
from uuid import uuid4

from .retention import CURSORS, REPORT, RetentionPolicy, backup_name, prune_expired


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write('\n')
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class ContinuousBackup:
    def __init__(self, repo, policy, receipt, seconds, *, now=None, clock=monotonic):
        if repo.backend != 'mysql' or repo.config.environment != 'PAPER_LOCAL' or repo._lease_depth != 1:
            raise ValueError('정상 종료된 모의매매의 독점 DB 잠금이 필요합니다.')
        if type(seconds) is not int or not 1 <= seconds <= 21600:
            raise ValueError('연속 정리는 최대 6시간입니다.')
        self.now = now or datetime.now(timezone.utc)
        self.name = backup_name(receipt, self.now)
        self.receipt = Path(receipt)
        self.text = self.receipt.read_bytes()
        self.repo, self.connection, self.policy = repo, repo.db, policy
        self.clock, self.started = clock, clock()
        self.deadline = self.started + seconds
        self.lease = 'pennyquant:' + sha256(f'{repo.config.database}:{repo.namespace}'.encode()).hexdigest()[:48]
        self.identity = repo._execute('SELECT CONNECTION_ID(),@@server_uuid', fetch='one')
        self.control = self._control()
        self.active = True
        self.verify(repo, policy, self.now)

    def _control(self):
        row = self.repo._execute('SELECT payload,updated_at FROM pq_controls WHERE namespace=%s AND name=%s',
                                (self.repo.namespace, 'trade_control_state'), fetch='one')
        value = json.loads(row[0]) if row and isinstance(row[0], str) else row[0] if row else {}
        uploaded = datetime.fromisoformat(self.text.decode().split()[1].replace('Z', '+00:00'))
        if (not row or value.get('mode') != 'paper' or value.get('status') != 'OFFLINE'
                or value.get('running') is not False or value.get('armed') is not False
                or row[1].replace(tzinfo=timezone.utc) > uploaded):
            raise ValueError('백업 이전에 정상 종료된 매매 상태를 확인하지 못했습니다.')
        return row

    def verify(self, repo, policy, now):
        if self.clock() >= self.deadline:
            raise MaintenanceDeadline('연속 정리 시간이 종료됐습니다.')
        if (not self.active or repo is not self.repo or repo.db is not self.connection
                or repo._lease_depth < 1 or repo._closed or policy != self.policy or now != self.now
                or self.receipt.read_bytes() != self.text):
            raise ValueError('연속 정리의 백업·시간·연결 조건이 변경됐습니다.')
        identity = repo._execute('SELECT CONNECTION_ID(),@@server_uuid,IS_USED_LOCK(%s)', (self.lease,), fetch='one')
        if identity[:2] != self.identity or identity[2] != self.identity[0] or self._control() != self.control:
            raise ValueError('연속 정리의 독점 잠금 또는 종료 상태가 변경됐습니다.')
        return self.name


class MaintenanceDeadline(TimeoutError):
    pass


def run(repo, policy, receipt, *, seconds=2700, now=None, clock=monotonic, progress=None):
    """Bounded retries keep committed cursors, never skip a failed source row."""
    if not policy.enabled:
        result = prune_expired(repo, policy, apply=True, receipt=receipt, now=now, clock=clock)
        if progress:
            atomic(progress, {'state': 'DISABLED'})
        return result
    with repo.engine_lease():
        grant = ContinuousBackup(repo, policy, receipt, seconds, now=now, clock=clock)
        identifier = uuid4().hex
        totals = {'scanned': 0, 'eligible': 0, 'deleted': 0}
        completed, table_totals, details, history = set(), {}, {}, []
        failures, passes, error_count, last = 0, 0, 0, None
        # An old cursor's cutoff may differ. Start a complete proof of this
        # frozen cutoff; previously deleted source rows stay deleted.
        repo.set_control(CURSORS, {})
        try:
            while clock() < grant.deadline:
                try:
                    current = prune_expired(repo, policy, apply=True, receipt=receipt, now=grant.now,
                                            clock=clock, _continuation=grant, _completed=completed)
                except TimeoutError:
                    if last is None or clock() < grant.deadline:
                        raise
                    break
                last = current
                passes += 1
                for key in totals:
                    totals[key] += last[key]
                for kind, row in last['tables'].items():
                    cumulative = table_totals.setdefault(kind, {key: 0 for key in ('scanned','eligible','deleted','protected')})
                    for key in cumulative:
                        cumulative[key] += row.get(key, 0)
                    previous = details.setdefault(kind, {})
                    timings = previous.get('timings', {})
                    for phase, sample in row.get('timings', {}).items():
                        total = timings.setdefault(phase, {'calls': 0, 'seconds': 0, 'max_seconds': 0, 'timeouts': 0})
                        for key in ('calls', 'seconds', 'timeouts'):
                            total[key] += sample.get(key, 0)
                        total['max_seconds'] = max(total['max_seconds'], sample.get('max_seconds', 0))
                    previous.update(row)
                    previous['timings'] = timings
                    if row.get('error'):
                        error_count += 1
                        history.append({'pass': passes, 'table': kind, 'error': row['error'],
                                        'batch': row.get('read_batch')})
                        history[:] = history[-10:]
                    else:
                        previous.pop('error', None)
                    if row['complete']:
                        completed.add(kind)
                failures = failures + 1 if last['errors'] else 0
                result = {**last, **totals, 'tables': {
                    kind: {**details[kind], **table_totals[kind]} for kind in last['tables']},
                    'maintenance': {'id': identifier, 'passes': passes, 'continuous_lease': True,
                                    'elapsed_seconds': round(clock()-grant.started, 3),
                                    'limit_seconds': seconds, 'consecutive_failures': failures,
                                    'read_errors': error_count, 'recent_read_errors': history[-10:]}}
                if progress:
                    atomic(progress, {'state': 'RUNNING', **{key: result[key] for key in ('status','scanned','deleted','errors','maintenance')}})
                if last['status'] in {'COMPLETE','DISABLED'} or failures >= 3:
                    break
            if last is None:
                raise ValueError('자료를 검사하기 전에 정리 시간이 종료됐습니다.')
            # COMPLETE requires every table scanned under one frozen cutoff and
            # the same live lease; optional sample statistics cannot prove it.
            if result['status'] == 'COMPLETE' and result['eligible'] != result['deleted']:
                result['status'] = 'PARTIAL'
                result['observability']['stop_reason'] = 'ROWS_CHANGED'
            if result['status'] == 'COMPLETE' and not result['errors']:
                result['observability'].update(backlog_status='CLEAR', eligible_backlog_min=0,
                                               attention='CLEAR', stop_reason='SESSION_COMPLETE')
            result['finished_at'] = datetime.now(timezone.utc).isoformat()
            repo.set_control(REPORT, result)
            if progress:
                atomic(progress, {'state': 'FAILED' if result['errors'] else result['status'],
                                  **{key: result[key] for key in ('status','scanned','deleted','errors','maintenance')}})
            return result
        except Exception as exc:
            if progress:
                atomic(progress, {'state': 'FAILED', **totals,
                                  'error': {'type': type(exc).__name__, 'code': getattr(exc, 'code', None)}})
            raise
        finally:
            grant.active = False


def main():
    import argparse
    from ..session_config import load_session_config
    from .config import MySQLConfig
    from .mysql_repository import MySQLRepository
    parser = argparse.ArgumentParser(description='수집 중지 상태에서 이어지는 자료 정리')
    parser.add_argument('--config', required=True)
    parser.add_argument('--backup-receipt', required=True)
    parser.add_argument('--seconds', type=int, required=True)
    parser.add_argument('--progress', required=True)
    args = parser.parse_args()
    config = load_session_config(args.config)
    db = MySQLConfig.from_env()
    if config.database_backend != 'mysql' or config.environment != 'PAPER_LOCAL' or db.environment not in {None, 'PAPER_LOCAL'}:
        raise ValueError('MySQL 모의매매 전용입니다.')
    policy = RetentionPolicy(**config.retention)
    with closing(MySQLRepository(config.db_path, replace(db, environment='PAPER_LOCAL'))) as repo:
        result = run(repo, policy, args.backup_receipt, seconds=args.seconds, progress=args.progress)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
