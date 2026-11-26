"""Real MySQL lease, cutoff, receipt and interrupted-cleanup regressions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os

import pytest

from test_mysql_integration import mysql_repositories  # noqa: F401
from test_retention_mysql import seed, receipt, POLICY
from sessionquant.database import maintenance
from sessionquant.database.retention import backup_name, CURSORS
from sessionquant.database.mysql_repository import MySQLRepositoryError

pytestmark = pytest.mark.skipif(os.getenv('PQ_TEST_MYSQL') != '1', reason='requires disposable MySQL 8')


def prepare(repo, tmp_path, *, age=0):
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    repo.set_control('trade_control_state', {'mode': 'paper', 'status': 'OFFLINE', 'running': False, 'armed': False})
    uploaded = now - timedelta(seconds=age)
    repo._execute('UPDATE pq_controls SET updated_at=%s WHERE namespace=%s AND name=%s',
                  ((uploaded-timedelta(seconds=1)).replace(tzinfo=None), repo.namespace, 'trade_control_state'))
    return now, receipt(tmp_path, uploaded)


def test_reduced_payload_batches_survive_continuous_pass_boundaries(mysql_repositories, tmp_path, monkeypatch):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now, proof = prepare(repo, tmp_path)
    for i in range(30):
        seed(repo, 'research_events', str(i), now, kind='engine_batch', run_id='closed', data={})
    execute, prune = repo._execute, maintenance.prune_expired
    calls, pass_number = [], [0]
    def limited(sql, params=(), **kwargs):
        if (sql.startswith('SELECT /*+') and 'JSON_OBJECT' in sql
                and 'FROM pq_research_events FORCE INDEX (PRIMARY)' in sql):
            size = len(params) - 1
            calls.append((pass_number[0], size))
            if size > 2:
                from pymysql.err import OperationalError
                raise MySQLRepositoryError(OperationalError(3024, 'fixture'), 'SELECT')
        return execute(sql, params, **kwargs)
    def counted(*args, **kwargs):
        pass_number[0] += 1
        return prune(*args, **kwargs)
    monkeypatch.setattr(repo, '_execute', limited)
    monkeypatch.setattr(maintenance, 'prune_expired', counted)
    result = maintenance.run(repo, replace(POLICY, intraday_auxiliary=True, batch_size=10, max_rows=10),
                             proof, now=now)
    assert result['status'] == 'COMPLETE' and result['deleted'] == 30
    assert any(p == 1 and size > 2 for p, size in calls)
    assert any(p > 1 for p, size in calls)
    assert all(size <= 2 for p, size in calls if p > 1)


def test_continuous_cleanup_crosses_receipt_hour_under_one_lease_and_preserves_finance(mysql_repositories, tmp_path, monkeypatch):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    competitor = mysql_repositories(repo.config.namespace, environment='PAPER_LOCAL')
    now, proof = prepare(repo, tmp_path, age=3599)
    for index in range(6):
        seed(repo, 'research_events', str(index), now, legacy=False, kind='engine_batch', run_id='closed', data={})
    seed(repo, 'research_events', 'linked', now, kind='engine_batch', run_id='closed', data={'news_id': 'financial'})
    seed(repo, 'signals', 'signal', now, news_id='financial')
    seed(repo, 'executions', 'execution', now, symbol='TSLA', qty='1')
    elapsed = [0.0]
    original = maintenance.prune_expired
    ids = []
    def inspect(*args, **kwargs):
        with pytest.raises(RuntimeError, match='다른 엔진'):
            with competitor.engine_lease():
                pytest.fail('another engine acquired maintenance lease')
        ids.append(repo._execute('SELECT CONNECTION_ID()', fetch='one')[0])
        result = original(*args, **kwargs)
        elapsed[0] += 1200
        return result
    monkeypatch.setattr(maintenance, 'prune_expired', inspect)
    policy = replace(POLICY, intraday_auxiliary=True, max_rows=2)
    progress = tmp_path/'progress.json'
    result = maintenance.run(repo, policy, proof, seconds=21600, now=now, clock=lambda: elapsed[0], progress=progress)
    assert result['status'] == 'COMPLETE' and result['deleted'] == 6
    assert result['observability']['backlog_status'] == 'CLEAR'
    assert result['maintenance']['passes'] > 1 and len(set(ids)) == 1
    assert result['tables']['research_events']['scanned'] == 7
    assert result['tables']['research_events']['timings']['DELETE']['calls'] == 3
    assert len(repo.all('research_events')) == len(repo.all('executions')) == len(repo.all('signals')) == 1
    assert json.loads(progress.read_text())['state'] == 'COMPLETE'
    with competitor.engine_lease():
        pass
    with pytest.raises(ValueError, match='1시간'):
        backup_name(proof, now+timedelta(seconds=elapsed[0]))


@pytest.mark.parametrize('change', ['receipt', 'control', 'unlock', 'server', 'connection', 'policy', 'cutoff', 'inactive'])
def test_grant_refuses_changed_evidence(mysql_repositories, tmp_path, monkeypatch, change):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now, proof = prepare(repo, tmp_path)
    with repo.engine_lease():
        grant = maintenance.ContinuousBackup(repo, POLICY, proof, 600, now=now)
        policy, cutoff = POLICY, now
        if change == 'receipt':
            proof.write_text(proof.read_text().replace('fixture', 'different'))
        elif change == 'control':
            repo.set_control('trade_control_state', {'mode': 'paper', 'status': 'OFFLINE', 'running': False, 'armed': False, 'changed': True})
        elif change == 'unlock':
            repo._execute('SELECT RELEASE_LOCK(%s)', (grant.lease,), fetch='one')
        elif change == 'server':
            grant.identity = (grant.identity[0], 'different-server')
        elif change == 'connection':
            grant.connection = object()
        elif change == 'policy':
            policy = replace(POLICY, batch_size=3)
        elif change == 'cutoff':
            cutoff += timedelta(days=1)
        else:
            grant.active = False
        with pytest.raises(ValueError):
            grant.verify(repo, policy, cutoff)


def test_new_worker_cannot_reuse_expired_receipt_or_backup_before_shutdown(mysql_repositories, tmp_path):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now, proof = prepare(repo, tmp_path, age=3601)
    with repo.engine_lease(), pytest.raises(ValueError, match='1시간'):
        maintenance.ContinuousBackup(repo, POLICY, proof, 600, now=now)
    now, proof = prepare(repo, tmp_path)
    repo._execute('UPDATE pq_controls SET updated_at=%s WHERE namespace=%s AND name=%s',
                  ((now+timedelta(seconds=1)).replace(tzinfo=None), repo.namespace, 'trade_control_state'))
    with repo.engine_lease(), pytest.raises(ValueError, match='백업 이전'):
        maintenance.ContinuousBackup(repo, POLICY, proof, 600, now=now)


def test_timeout_commits_only_verified_prefix_and_never_skips_failed_row(mysql_repositories, tmp_path, monkeypatch):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now, proof = prepare(repo, tmp_path)
    for index in range(25):
        seed(repo, 'research_events', str(index), now, kind='engine_batch', run_id='closed', data={})
    ids = repo._execute('SELECT seq FROM pq_research_events WHERE namespace=%s ORDER BY seq', (repo.namespace,), fetch='all')
    failed = ids[10][0]
    original = repo._execute
    def fail(sql, params=(), **kwargs):
        if sql.startswith('SELECT /*+') and 'JSON_OBJECT' in sql and 'pq_research_events' in sql and failed in params:
            from pymysql.err import OperationalError
            raise MySQLRepositoryError(OperationalError(3024, 'fixture timeout'), 'SELECT')
        return original(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', fail)
    result = maintenance.run(repo, replace(POLICY, intraday_auxiliary=True, batch_size=200), proof, now=now)
    assert result['status'] == 'PARTIAL' and result['deleted'] == 10
    assert result['maintenance']['consecutive_failures'] == 3
    assert result['errors'][0]['code'] == 3024
    assert len(repo.all('research_events')) == 15
    assert original('SELECT seq FROM pq_research_events WHERE namespace=%s ORDER BY seq LIMIT 1',
                    (repo.namespace,), fetch='one')[0] == failed
    cursor = repo.get_control(CURSORS)['positions']['research_events']
    assert cursor[1] == ids[9][0]


def test_deadline_keeps_partial_progress_without_new_backup(mysql_repositories, tmp_path, monkeypatch):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now, proof = prepare(repo, tmp_path)
    for index in range(4):
        seed(repo, 'research_events', str(index), now, kind='engine_batch', run_id='closed', data={})
    elapsed = [0]
    original = maintenance.prune_expired
    def pass_then_expire(*args, **kwargs):
        result = original(*args, **kwargs)
        elapsed[0] = 601
        return result
    monkeypatch.setattr(maintenance, 'prune_expired', pass_then_expire)
    result = maintenance.run(repo, replace(POLICY, intraday_auxiliary=True, max_rows=2), proof,
                             seconds=600, now=now, clock=lambda: elapsed[0])
    assert result['status'] == 'PARTIAL' and not result['errors'] and result['deleted'] == 2
    assert result['maintenance']['passes'] == 1
    assert len(repo.all('research_events')) == 2


def test_inactive_policy_is_noop_without_control_or_receipt(mysql_repositories, tmp_path):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    result = maintenance.run(repo, replace(POLICY, enabled=False), None, progress=tmp_path/'progress.json')
    assert result['status'] == 'DISABLED' and result['deleted'] == 0


def test_recovered_read_error_remains_in_history_but_not_current_errors(mysql_repositories, tmp_path, monkeypatch):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now, proof = prepare(repo, tmp_path)
    seed(repo, 'research_events', 'recover', now, kind='engine_batch', run_id='closed', data={})
    execute, prune = repo._execute, maintenance.prune_expired
    passes = [0]
    def transient(sql, params=(), **kwargs):
        if passes[0] == 1 and sql.startswith('SELECT /*+') and 'JSON_OBJECT' in sql and 'pq_research_events' in sql:
            from pymysql.err import OperationalError
            raise MySQLRepositoryError(OperationalError(3024, 'fixture timeout'), 'SELECT')
        return execute(sql, params, **kwargs)
    def counted(*args, **kwargs):
        passes[0] += 1
        return prune(*args, **kwargs)
    monkeypatch.setattr(repo, '_execute', transient)
    monkeypatch.setattr(maintenance, 'prune_expired', counted)
    result = maintenance.run(repo, replace(POLICY, intraday_auxiliary=True), proof, now=now)
    assert result['status'] == 'COMPLETE' and result['deleted'] == 1 and not result['errors']
    assert 'error' not in result['tables']['research_events']
    assert result['maintenance']['read_errors'] == 1
    assert result['maintenance']['recent_read_errors'][0]['error']['code'] == 3024
    assert result['tables']['research_events']['timings']['PAYLOAD']['timeouts'] == 2


def test_lost_connection_revokes_grant_and_releases_engine_lease(mysql_repositories, tmp_path):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    other = mysql_repositories(repo.config.namespace, environment='PAPER_LOCAL')
    now, proof = prepare(repo, tmp_path)
    with repo.engine_lease():
        grant = maintenance.ContinuousBackup(repo, POLICY, proof, 600, now=now)
        repo.close()
        with pytest.raises(ValueError):
            grant.verify(repo, POLICY, now)
    with other.engine_lease():
        pass
