"""Opt-in indexed audit selection parity using a disposable real MySQL 8."""
import json
import os

import pytest

from test_mysql_integration import mysql_repositories  # noqa: F401
from sessionquant.database import llm_attempt_index as index

pytestmark = pytest.mark.skipif(os.getenv('PQ_TEST_MYSQL') != '1', reason='requires disposable MySQL 8')


def raw(repo, body):
    repo._execute('INSERT INTO pq_llm_calls(namespace,payload,captured_at) VALUES(%s,%s,CURRENT_TIMESTAMP(6))',
                  (repo.namespace, json.dumps(body)))


def source(repo):
    return repo._execute('SELECT seq,updated_at FROM pq_llm_calls WHERE namespace=%s ORDER BY seq',
                         (repo.namespace,), fetch='all')


def test_json_types_legacy_parity_and_fully_indexed_selection(mysql_repositories, monkeypatch):
    repo, other = mysql_repositories(), mysql_repositories()
    assert index.trigger_signature(repo), 'CI must enable actual expected triggers'
    for number, value in enumerate([True, False, None, 1, 0, 'true', 'false', {}, [], [True], True]):
        raw(repo, {'id': str(number), 'network_attempted': value})
    raw(repo, {'id': 'missing'})
    raw(other, {'id': 'private', 'network_attempted': True})
    expected = index._legacy(repo, 500)
    assert index.prove_coverage(repo, batch_size=3)
    calls = []
    execute = repo._execute
    def tracked(sql, *args, **kwargs):
        calls.append(sql)
        return execute(sql, *args, **kwargs)
    monkeypatch.setattr(repo, '_execute', tracked)
    assert repo.recent_llm_attempts(500) == expected
    assert not any('JSON_EXTRACT' in sql for sql in calls)
    assert any('FORCE INDEX (ix_namespace_attempt_seq)' in sql for sql in calls)
    assert all(row['id'] != 'private' for row in expected)
    assert repo.recent_llm_attempts(1) == expected[-1:]
    plan = execute(f'EXPLAIN SELECT source_seq FROM {index.TABLE} FORCE INDEX (ix_namespace_attempt_seq) '
                   'WHERE namespace=%s AND network_attempted=1 ORDER BY source_seq DESC LIMIT 500',
                   (repo.namespace,), fetch='one')
    assert plan[5] == 'ix_namespace_attempt_seq'
    assert index.TABLE in {row['table'] for row in repo.storage_stats()['tables']}


def test_sparse_attempt_history_has_no_fixed_scan_truncation(mysql_repositories):
    repo = mysql_repositories()
    raw(repo, {'id': 'older-paid', 'network_attempted': True})
    placeholders = ','.join('(%s,%s,CURRENT_TIMESTAMP(6))' for _ in range(6000))
    repo._execute('INSERT INTO pq_llm_calls(namespace,payload,captured_at) VALUES ' + placeholders,
                  tuple(v for n in range(6000) for v in (repo.namespace, json.dumps({'id':str(n),'network_attempted':False}))))
    raw(repo, {'id': 'newer-paid', 'network_attempted': True})
    expected = [{'id': 'older-paid', 'network_attempted': True}, {'id': 'newer-paid', 'network_attempted': True}]
    assert repo.recent_llm_attempts(500) == expected  # no receipt: full correct legacy query
    assert index.prove_coverage(repo)
    assert repo.recent_llm_attempts(500) == expected


def test_raw_update_upsert_delete_and_rollback_keep_index_atomic(mysql_repositories):
    repo = mysql_repositories()
    repo.record('llm_calls', {'id': 'call', 'network_attempted': False})
    seq, stamp = source(repo)[0]
    assert index.prove_coverage(repo)
    repo._execute("UPDATE pq_llm_calls SET payload=JSON_SET(payload,'$.network_attempted',CAST('true' AS JSON)),updated_at=%s WHERE seq=%s",
                  (stamp, seq))
    assert repo.recent_llm_attempts(500) == [{'id': 'call', 'network_attempted': True}]
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo._execute("UPDATE pq_llm_calls SET payload=JSON_SET(payload,'$.network_attempted',CAST('false' AS JSON)) WHERE seq=%s", (seq,))
            assert repo.recent_llm_attempts(500) == []
            raise RuntimeError('rollback')
    assert repo.recent_llm_attempts(500)[0]['network_attempted'] is True
    repo.record('llm_calls', {'id': 'call', 'network_attempted': False})
    assert repo.recent_llm_attempts(500) == []
    repo._execute('DELETE FROM pq_llm_calls WHERE namespace=%s', (repo.namespace,))
    assert repo._execute(f'SELECT 1 FROM {index.TABLE} WHERE source_seq=%s', (seq,), fetch='one') is None


def test_legacy_missing_false_row_rejects_proof_and_bounded_backfill_repairs(mysql_repositories):
    repo, other = mysql_repositories(), mysql_repositories()
    for value in (False, True, False, True, False):
        raw(repo, {'network_attempted': value})
    raw(other, {'network_attempted': True})
    seq = source(repo)[0][0]
    repo._execute(f'DELETE FROM {index.TABLE} WHERE source_seq=%s', (seq,))
    assert not index.prove_coverage(repo, batch_size=2)
    assert repo.get_control(index.CONTROL)['complete'] is False
    expected = index._legacy(repo, 500)
    first = index.backfill_page(repo, limit=2)
    assert first['rows'] == 2 and not first['done']
    assert repo.get_control(index.CONTROL)['complete'] is False
    assert repo.recent_llm_attempts(500) == expected
    second = index.backfill_page(repo, after=first['after'], limit=2)
    third = index.backfill_page(repo, after=second['after'], limit=2)
    assert third['rows'] == 1 and third['done']
    assert index.prove_coverage(repo, batch_size=2)
    assert repo.recent_llm_attempts(500) == expected
    assert other.get_control(index.CONTROL) is None


def test_stale_selected_projection_and_bad_receipts_fall_back(mysql_repositories, monkeypatch):
    repo = mysql_repositories()
    raw(repo, {'id': 'call', 'network_attempted': True})
    assert index.prove_coverage(repo)
    receipt = repo.get_control(index.CONTROL)
    expected = index._legacy(repo, 500)
    for changed in ({'namespace': 'other'}, {'version': 2}, {'trigger_signature': 'old-installation'}, {'complete': False}):
        repo.set_control(index.CONTROL, {**receipt, **changed})
        assert repo.recent_llm_attempts(500) == expected
    repo.set_control(index.CONTROL, receipt)
    repo._execute(f'UPDATE {index.TABLE} SET source_updated_at=DATE_SUB(source_updated_at, INTERVAL 1 SECOND) WHERE namespace=%s', (repo.namespace,))
    assert repo.recent_llm_attempts(500) == expected
    monkeypatch.setattr(index, 'trigger_signature', lambda _: None)
    assert repo.recent_llm_attempts(500) == expected
