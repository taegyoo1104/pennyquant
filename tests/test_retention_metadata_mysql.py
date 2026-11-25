"""Opt-in regressions for the transactional research retention projection."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from time import monotonic

import pytest

from test_mysql_integration import mysql_repositories  # noqa: F401
from test_retention_mysql import seed, receipt, POLICY
from sessionquant.database import retention_metadata as meta
from sessionquant.database.retention import _projection, _read_payload_page, prune_expired

pytestmark = pytest.mark.skipif(os.getenv('PQ_TEST_MYSQL') != '1', reason='requires disposable MySQL 8')


def source(repo, key):
    return repo._execute('SELECT seq,captured_at,created_at,updated_at FROM pq_research_events '
                         'WHERE namespace=%s AND record_id=%s', (repo.namespace, key), fetch='one')


def cached(repo):
    return repo._execute(f'SELECT source_seq,source_updated_at,metadata FROM {meta.TABLE} '
                         'WHERE namespace=%s ORDER BY source_seq', (repo.namespace,), fetch='all')


@pytest.mark.parametrize('value', [None, False, 17, '2026-11-20T10:00:00+00:00', '잘못된 시각', ['time'], {'time': 'bad'}])
def test_new_projection_preserves_mysql_json_types_and_original_dates(mysql_repositories, value):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    assert repo.retention_metadata_available(), 'CI must enable the real transactional trigger path'
    seed(repo, 'research_events', 'sample', datetime.now(timezone.utc), legacy=False,
         kind='engine_batch', run_id='닫힌 실행', data={'received_at': value, 'bulk': 'x' * 1700000})
    row = source(repo, 'sample')
    projected = meta.read(repo, [row[:2]], "payload->>'$.kind'='engine_batch'")[0]
    original = repo._execute(f'SELECT {_projection("research_events")} FROM pq_research_events WHERE seq=%s',
                             (row[0],), fetch='one')[0]
    assert json.loads(projected[4]) == json.loads(original)
    assert projected[:4] == row and projected[5] == 1
    assert len(projected[4].encode()) < 2000


def test_raw_writes_refresh_even_unchanged_timestamp_and_roll_back_together(mysql_repositories):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now = datetime.now(timezone.utc)
    seed(repo, 'research_events', 'update', now, legacy=False, kind='engine_batch', data={'news_id': 'first'})
    before = source(repo, 'update')
    repo._execute("UPDATE pq_research_events SET payload=JSON_SET(payload,'$.data.news_id','second'),updated_at=%s WHERE seq=%s",
                  (before[3], before[0]))
    assert source(repo, 'update') == before
    assert json.loads(cached(repo)[0][2])['news_id'] == 'second'
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo._execute("UPDATE pq_research_events SET payload=JSON_SET(payload,'$.data.news_id','rollback') WHERE seq=%s", (before[0],))
            assert json.loads(cached(repo)[0][2])['news_id'] == 'rollback'
            raise RuntimeError('abort transaction')
    assert json.loads(cached(repo)[0][2])['news_id'] == 'second'
    repo._execute('DELETE FROM pq_research_events WHERE namespace=%s', (repo.namespace,))
    assert not cached(repo)


def test_oversized_projection_cannot_leave_stale_cache_and_backfill_is_namespace_scoped(mysql_repositories):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    other = mysql_repositories(environment='PAPER_LOCAL')
    now = datetime.now(timezone.utc)
    seed(repo, 'research_events', 'sample', now, legacy=False, kind='engine_batch', run_id='closed', data={})
    seed(other, 'research_events', 'other', now, kind='engine_batch', run_id='closed', data={})
    row = source(repo, 'sample')
    repo._execute("UPDATE pq_research_events SET payload=JSON_SET(payload,'$.data.received_at',%s),updated_at=%s WHERE seq=%s",
                  ('x' * 20000, row[3], row[0]))
    assert not cached(repo)
    meta.backfill(repo, [row, source(other, 'other')])
    assert not cached(repo) and not cached(other)
    table = {}
    rows = _read_payload_page(repo, 'research_events', '1=1', [row[:2]], table, monotonic()+20, monotonic, cache_writes=True)
    assert len(json.loads(rows[0][4])['received_at']) == 20000
    assert table['metadata_hits'] == 0 and not cached(repo)


def test_cached_cleanup_uses_small_point_reads_and_keeps_financial_active_and_bad_dates(mysql_repositories, tmp_path, monkeypatch):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now = datetime.now(timezone.utc)
    for key, run, data in [('expired', 'closed', {}), ('active', 'current', {}),
                           ('finance', 'closed', {'news_id': 'linked'}), ('bad-date', 'closed', {'received_at': False})]:
        seed(repo, 'research_events', key, now, legacy=False, kind='engine_batch', run_id=run,
             data={'bulk': 'x' * 1700000, **data})
    repo.set_control('research_recording_v1', {'active': True, 'run_id': 'current'})
    seed(repo, 'signals', 'signal', now, news_id='linked')
    execute, plans = repo._execute, []
    def verify(sql, params=(), **kwargs):
        if sql.startswith('SELECT /*+ MAX_EXECUTION_TIME(3000)') and 'pq_research_events' in sql:
            assert 'JSON_OBJECT' not in sql
            if 'STRAIGHT_JOIN' in sql:
                plan = json.loads(execute('EXPLAIN FORMAT=JSON ' + sql, params, fetch='one')[0])
                tables = [x['table'] for x in plan['query_block']['nested_loop']]
                assert all(t['key'] == 'PRIMARY' for t in tables)
                assert all(t['access_type'] in {'range', 'const', 'eq_ref', 'system'} for t in tables)
                plans.append(plan)
        return execute(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', verify)
    result = prune_expired(repo, replace(POLICY, intraday_auxiliary=True), now=now,
                           apply=True, receipt=receipt(tmp_path, now))
    assert result['status'] == 'COMPLETE' and result['deleted'] == 1 and not result['errors']
    assert {r['id'] for r in repo.all('research_events')} == {'active', 'finance', 'bad-date'}
    assert len(cached(repo)) == 3 and plans
    assert result['tables']['research_events']['metadata_hits'] == 4


def test_legacy_dry_run_does_not_backfill_then_checked_apply_reuses_it(mysql_repositories):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    seed(repo, 'research_events', 'legacy', datetime.now(timezone.utc), kind='engine_batch', run_id='closed', data={})
    row = source(repo, 'legacy')
    dry_table = {}
    first = _read_payload_page(repo, 'research_events', '1=1', [row[:2]], dry_table, monotonic()+20, monotonic)
    assert not cached(repo)
    table = {}
    second = _read_payload_page(repo, 'research_events', '1=1', [row[:2]], table, monotonic()+20, monotonic, cache_writes=True)
    assert second == first and table['metadata_backfill_checked'] == 1 and len(cached(repo)) == 1
    last_table = {}
    assert meta.available(repo)
    assert _read_payload_page(repo, 'research_events', '1=1', [row[:2]], last_table, monotonic()+20, monotonic) == first
    assert last_table['metadata_hits'] == 1 and 'PAYLOAD' not in last_table['timings']


def test_metadata_write_timeout_preserves_original_checked_cleanup(mysql_repositories, tmp_path, monkeypatch):
    from sessionquant.database.mysql_repository import MySQLRepositoryError
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now = datetime.now(timezone.utc)
    seed(repo, 'research_events', 'legacy', now, kind='engine_batch', run_id='closed', data={})
    execute = repo._execute
    def slow_cache(sql, *args, **kwargs):
        if 'retention metadata refresh' in sql:
            raise MySQLRepositoryError(Exception(3024, 'fixture timeout'), 'SELECT')
        return execute(sql, *args, **kwargs)
    monkeypatch.setattr(repo, '_execute', slow_cache)
    result = prune_expired(repo, replace(POLICY, intraday_auxiliary=True), now=now,
                           apply=True, receipt=receipt(tmp_path, now))
    assert result['status'] == 'COMPLETE' and result['deleted'] == 1 and not result['errors']
    assert result['tables']['research_events']['metadata_write_timeouts'] == 1
    assert result['tables']['research_events']['last_read_timeout']['phase'] == 'METADATA_WRITE'
    assert not cached(repo) and not repo.all('research_events')


def test_startup_probe_reads_cached_protection_without_source_body_or_writes(mysql_repositories, monkeypatch):
    from sessionquant.database.retention_probe import probe_retention
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now = datetime.now(timezone.utc)
    seed(repo, 'research_events', 'protected', now, legacy=False, kind='engine_batch', run_id='closed',
         data={'bulk': 'x' * 1700000, 'received_at': 'unknown'})
    execute = repo._execute
    def read_only(sql, params=(), **kwargs):
        assert sql.startswith('SELECT')
        if 'pq_research_events' in sql:
            assert 'JSON_OBJECT' not in sql
        return execute(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', read_only)
    result = probe_retention(repo, replace(POLICY, intraday_auxiliary=True), now=now)
    assert result['status'] == 'CLEAR'
    assert result['tables']['research_events']['metadata_hits'] == 1
