from types import SimpleNamespace

import pytest

from sessionquant.database import pipeline_summary as cached
from sessionquant.database.mysql_repository import MySQLRepositoryError


class Repo:
    config = SimpleNamespace(database='fixture')
    namespace = 'fixture_namespace'

    def __init__(self, code=None, existing=(), fail_stage='CREATE TRIGGER'):
        self.calls = []
        self.code, self.existing, self.fail_stage = code, existing, fail_stage

    def _execute(self, sql, params=(), **kwargs):
        self.calls.append((sql, params))
        if self.code and sql.startswith(self.fail_stage):
            raise MySQLRepositoryError(Exception(self.code, 'fixture'), 'QUERY')
        if sql.startswith('SELECT TRIGGER_NAME'):
            return [(name,) for name in self.existing]
        return 0


@pytest.mark.parametrize('code', [1142, 1143, 1227, 1419])
@pytest.mark.parametrize('fail_stage', ['CREATE TABLE', 'CREATE TRIGGER'])
def test_optional_summary_permission_failure_keeps_original_path(code, fail_stage):
    repo = Repo(code, fail_stage=fail_stage)
    cached.install(repo)
    assert not cached.available(repo)
    assert not any(sql.startswith(('DELETE FROM pq_', 'UPDATE pq_')) for sql, _ in repo.calls)


def test_installation_is_additive_and_update_trigger_precedes_insert():
    repo = Repo()
    cached.install(repo)
    assert cached.available(repo)
    triggers = [sql for sql, _ in repo.calls if sql.startswith('CREATE TRIGGER')]
    assert 'AFTER UPDATE' in triggers[0] and 'AFTER INSERT' in triggers[1]
    assert all('NEW.payload' in sql for sql in triggers)
    assert not any(sql.startswith(('ALTER', 'UPDATE pq_', 'DELETE FROM pq_')) for sql, _ in repo.calls)
    existing = Repo(existing=cached.TRIGGERS)
    cached.install(existing)
    assert not any(sql.startswith('CREATE TRIGGER') for sql, _ in existing.calls)


def test_unknown_error_is_not_reported_as_optional_unavailability():
    repo = Repo(2013)
    with pytest.raises(MySQLRepositoryError):
        cached.install(repo)
    assert not hasattr(repo, '_pipeline_summary_available')


def test_missing_cache_only_projects_selected_legacy_rows():
    repo = Repo()
    repo._pipeline_summary_available = True
    calls = []

    def execute(sql, params=(), **kwargs):
        calls.append((sql, params))
        if sql.startswith('SELECT seq FROM'):
            return [(30,), (29,), (28,)]
        if sql.startswith('SELECT r.seq'):
            return [(30, 'new'), (28, 'older')]
        assert sql.startswith('SELECT seq,JSON_ARRAY(')
        assert params == (repo.namespace, 29)
        return [(29, 'legacy')]

    repo._execute = execute
    from unittest.mock import patch
    with patch.object(cached, 'summary', lambda raw: raw):
        assert cached.recent(repo, 3) == ['older', 'legacy', 'new']
    assert calls[0][1] == (repo.namespace, 3)
    assert 'payload' not in calls[1][0]
    assert all(params[0] == repo.namespace for _, params in calls)


def test_summary_select_denied_uses_bounded_original_projection():
    repo = Repo()
    repo._pipeline_summary_available = True

    def execute(sql, params=(), **kwargs):
        if sql.startswith('SELECT seq FROM'):
            return [(4,)]
        if sql.startswith('SELECT r.seq'):
            raise MySQLRepositoryError(Exception(1142, 'fixture'), 'SELECT')
        assert params == (repo.namespace, 4)
        return []

    repo._execute = execute
    assert cached.recent(repo, 1) == []
    assert not cached.available(repo)
