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


def test_timing_keeps_recovered_timeout_context_after_later_success(monkeypatch):
    from sessionquant.database import retention
    ticks = iter(i / 8 for i in range(10))
    monkeypatch.setattr(retention, 'monotonic', lambda: next(ticks))
    repo = Repo(size=2)
    table = {}
    policy_ticks = []

    def policy_clock():
        policy_ticks.append(0)
        return 0

    assert _read_payload_page(repo, 'research_events', '1=1',
        [(i, None) for i in range(4)], table, 90, policy_clock) == list(range(4))
    assert table['timings']['PAYLOAD'] == {
        'calls': 3, 'seconds': .375, 'max_seconds': .125, 'timeouts': 1}
    assert table['last_read_timeout'] == {
        'phase': 'PAYLOAD', 'code': 3024, 'count': 4, 'first_seq': 0, 'last_seq': 3}
    assert table['read_batch'] == {'count': 2, 'first_seq': 2, 'last_seq': 3}
    assert len(policy_ticks) == 3  # Timing does not consume the cleanup budget clock.
    _read_payload_page(repo, 'research_events', '1=1', [(42, None)], table, 90, policy_clock)
    assert table['timings']['PAYLOAD']['calls'] == 4
    assert table['timings']['PAYLOAD']['timeouts'] == 1
    assert table['last_read_timeout']['first_seq'] == 0
    assert 'private SQL' not in str(table)


def test_non_timeout_error_is_measured_and_propagated(monkeypatch):
    from sessionquant.database import retention
    ticks = iter([1.0, 1.5])
    monkeypatch.setattr(retention, 'monotonic', lambda: next(ticks))
    table = {}
    with pytest.raises(MySQLRepositoryError) as caught:
        _read_payload_page(Repo(fatal=2013), 'research_events', '1=1',
            [(42, None)], table, 90, lambda: 0)
    assert caught.value.code == 2013
    assert table['timings']['PAYLOAD'] == {
        'calls': 1, 'seconds': .5, 'max_seconds': .5, 'timeouts': 0}
    assert 'last_read_timeout' not in table
