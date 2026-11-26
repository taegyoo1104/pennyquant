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
from sessionquant.database.retention import _projection, _projection_paths, _read_payload_page, prune_expired

pytestmark = pytest.mark.skipif(os.getenv('PQ_TEST_MYSQL') != '1', reason='requires disposable MySQL 8')


@pytest.mark.parametrize('kind', ['research_events', 'pipeline_status'])
@pytest.mark.parametrize('value', [None, False, 17, '', 'null', ['bad'], {'bad': 'time'},
                                 '2026-11-26T16:00:00+00:00', 'x' * 20000])
def test_single_parse_projection_matches_old_json_types_on_large_documents(mysql_repositories, kind, value):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    data = {key: value for key in ('id', 'news_id', 'received_at', 'valid_until')}
    data['news_contexts'] = {'AAPL': {'body': 'x' * 2200000}}
    body = {'kind': 'engine_batch', 'run_id': 'closed', 'data': data} if kind == 'research_events' else data
    paths = _projection_paths(kind)
    old = 'JSON_OBJECT(' + ','.join(f"'{k}',JSON_EXTRACT(payload,'{p}')" for k, p in paths.items()) + ')'
    new = _projection(kind)
    row = repo._execute(f'SELECT {old},{new} FROM (SELECT CAST(%s AS JSON) AS payload) fixture',
                        (json.dumps(body),), fetch='one')
    assert json.loads(row[0]) == json.loads(row[1])


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
    seed(repo, 'research_events', 'protected', now, kind='engine_batch', run_id='closed',
         data={'received_at': 'unknown'})
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
    assert not cached(repo) and [r['id'] for r in repo.all('research_events')] == ['protected']


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


def test_expired_legacy_rows_are_not_cached_just_before_deletion(mysql_repositories, tmp_path):
    repo = mysql_repositories(environment='PAPER_LOCAL')
    now = datetime.now(timezone.utc)
    for index in range(12):
        seed(repo, 'research_events', str(index), now, kind='engine_batch', run_id='closed', data={})
    result = prune_expired(repo, replace(POLICY, batch_size=12, intraday_auxiliary=True), now=now,
                           apply=True, receipt=receipt(tmp_path, now))
    assert result['deleted'] == 12 and not result['errors']
    table = result['tables']['research_events']
    assert table.get('metadata_backfill_checked', 0) == 0
    assert 'METADATA_WRITE' not in table['timings'] and not cached(repo)


@pytest.mark.parametrize('mixed', [False, True])
def test_optional_observations_reuse_valid_metadata_and_fallback_only_for_missing_keys(mysql_repositories, monkeypatch, mixed):
    from dataclasses import asdict
    from datetime import timedelta
    from sessionquant.database import retention
    repo = mysql_repositories(environment='PAPER_LOCAL')
    other = mysql_repositories(environment='PAPER_LOCAL')
    now = datetime.now(timezone.utc)
    for key in ('valid', 'missing', 'stale', 'wrong-namespace'):
        seed(repo, 'research_events', key, now, legacy=False, kind='engine_batch', run_id='closed',
             data={'bulk': 'x' * 1700000, **({'news_id': 'protected'} if key in {'stale', 'wrong-namespace'} else {})})
    seed(other, 'research_events', 'other', now, legacy=False, kind='engine_batch', run_id='closed', data={})
    ids = {key: source(repo, key)[0] for key in ('valid', 'missing', 'stale', 'wrong-namespace')}
    if mixed:
        repo._execute(f'DELETE FROM {meta.TABLE} WHERE source_seq=%s', (ids['missing'],))
        repo._execute(f'UPDATE {meta.TABLE} SET source_updated_at=source_updated_at + INTERVAL 1 SECOND WHERE source_seq=%s',
                      (ids['stale'],))
        repo._execute(f'UPDATE {meta.TABLE} SET namespace=%s WHERE source_seq=%s', (other.namespace, ids['wrong-namespace']))
    monkeypatch.setattr(retention, 'retention_rules', lambda _: (
        ('research_events', 'diagnostics_days', "payload->>'$.kind'='engine_batch'"),))
    report = {'policy': asdict(POLICY), 'status': 'COMPLETE', 'scanned': 0,
              'deleted': 0, 'errors': [], 'tables': {'research_events': {'deleted': 0}}}
    previous = {'tables': {'research_events': {'observation': {'generation': {
        'high_water_seq': 0, 'measured_at': (now - timedelta(minutes=1)).isoformat()}}}}}
    execute, fallback_keys = repo._execute, []
    def observe_only(sql, params=(), **kwargs):
        assert sql.startswith('SELECT') and kwargs.get('optional_observation')
        assert 'MAX_EXECUTION_TIME(' in sql and params[0] == repo.namespace
        if 'payload' in sql:
            assert mixed and 'FORCE INDEX (PRIMARY)' in sql and 'seq IN (' in sql
            fallback_keys.extend(params[1:])
            assert ids['valid'] not in params[1:]
        return execute(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', observe_only)
    retention._observe_retention(repo, report, previous, {'news': {'protected'}, 'selection': set()},
        {'research_events': {'highest': 0, 'new_min': 0, 'exact': True}}, monotonic()+30, monotonic, now)
    obs = report['tables']['research_events']['observation']
    assert obs['status'] == 'COMPLETE' and obs['backlog_status'] == 'PRESENT'
    assert obs['eligible_rows_min'] == 2 and obs['backlog_exact']
    assert obs['generation']['rows_min'] == 4 and obs['generation']['exact']
    assert obs['metadata_hits'] == (2 if mixed else 8)
    assert report['observability']['observation_scanned'] == 17  # endpoint + both key/row samples
    assert set(fallback_keys) == ({ids[x] for x in ('missing', 'stale', 'wrong-namespace')} if mixed else set())
    assert len(fallback_keys) == (6 if mixed else 0)  # backlog and generation both use only missing keys
