from types import SimpleNamespace

import pytest

from sessionquant.database import retention_metadata as meta
from sessionquant.database.mysql_repository import MySQLRepositoryError


class Repo:
    config = SimpleNamespace(database='fixture')

    def __init__(self, code=None, existing=()):
        self.calls = []
        self.code, self.existing = code, existing

    def _execute(self, sql, *args, **kwargs):
        self.calls.append(sql)
        if sql.startswith('SELECT TRIGGER_NAME'):
            return [(name,) for name in self.existing]
        if sql.startswith('CREATE TRIGGER') and self.code:
            raise MySQLRepositoryError(Exception(self.code, 'fixture'), 'CREATE')
        return 0


@pytest.mark.parametrize('code', [1142, 1227, 1419])
def test_trigger_permission_failure_keeps_legacy_path(code):
    repo = Repo(code)
    meta.install(repo)
    assert not meta.available(repo)
    assert not meta.supports(repo, 'research_events')
    assert not any('DELETE FROM pq_' in sql or 'UPDATE pq_' in sql for sql in repo.calls)


def test_trigger_installation_orders_update_before_insert_and_is_idempotent():
    repo = Repo()
    meta.install(repo)
    triggers = [sql for sql in repo.calls if sql.startswith('CREATE TRIGGER')]
    assert 'AFTER UPDATE' in triggers[0] and 'AFTER INSERT' in triggers[1]
    assert meta.available(repo)
    repo = Repo(existing=meta.TRIGGERS)
    meta.install(repo)
    assert not any(sql.startswith('CREATE TRIGGER') for sql in repo.calls)


def test_unknown_schema_error_does_not_silently_enable_optimization():
    repo = Repo(2013)
    with pytest.raises(MySQLRepositoryError):
        meta.install(repo)
    assert not hasattr(repo, '_research_retention_metadata')
