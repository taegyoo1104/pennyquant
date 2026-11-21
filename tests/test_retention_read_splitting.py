import pytest
import pymysql

from sessionquant.database.mysql_repository import MySQLRepositoryError
from sessionquant.database.retention import _read_payload_page


class Repo:
    namespace = 'fixture'

    def __init__(self, *, fatal=None, size=2):
        self.calls = []
        self.fatal = fatal
        self.size = size

    def _execute(self, sql, params, **kwargs):
        assert sql.startswith('SELECT /*+ MAX_EXECUTION_TIME(3000)')
        assert 'FORCE INDEX (PRIMARY)' in sql
        ids = list(params[1:])
        self.calls.append(ids)
        if self.fatal or len(ids) > self.size:
            raise MySQLRepositoryError(pymysql.err.OperationalError(self.fatal or 3024, 'private SQL'), 'SELECT')
        return ids


def test_split_preserves_every_key_and_order_without_raising_timeout_limit():
    repo = Repo()
    table = {}
    result = _read_payload_page(repo, 'research_events', '1=1', [(i, None) for i in range(10)], table, 90, lambda: 0)
    assert result == list(range(10)) and table['read_splits'] > 0
    assert max(map(len, repo.calls)) == 10
    assert table['read_phase'] == 'PAYLOAD'


def test_individual_record_timeout_still_fails_without_skipping_it():
    repo = Repo(size=0)
    table = {}
    with pytest.raises(MySQLRepositoryError) as caught:
        _read_payload_page(repo, 'research_events', '1=1', [(42, None), (43, None)], table, 90, lambda: 0)
    assert caught.value.code == 3024 and repo.calls == [[42, 43], [42]]
    assert table['read_batch'] == {'count': 1, 'first_seq': 42, 'last_seq': 42}


def test_split_stops_at_global_deadline_and_does_not_return_partial_page():
    repo = Repo()
    times = iter([0, 0, 91])
    with pytest.raises(TimeoutError):
        _read_payload_page(repo, 'research_events', '1=1', [(i, None) for i in range(4)], {}, 90, lambda: next(times))
    assert repo.calls == [[0, 1, 2, 3], [0, 1]]


def test_other_database_errors_are_not_retried():
    repo = Repo(fatal=2013)
    with pytest.raises(MySQLRepositoryError) as caught:
        _read_payload_page(repo, 'research_events', '1=1', [(42, None), (43, None)], {}, 90, lambda: 0)
    assert caught.value.code == 2013 and repo.calls == [[42, 43]]
