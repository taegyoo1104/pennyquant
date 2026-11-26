import pytest
import pymysql
import re

from sessionquant.database.mysql_repository import MySQLRepositoryError
from sessionquant.database.retention import _read_payload_page


class Repo:
    namespace = 'fixture'

    def __init__(self, *, fatal=None, size=2):
        self.calls = []
        self.fatal = fatal
        self.size = size
        self.limits = []

    def _execute(self, sql, params, **kwargs):
        limit = int(re.search(r'MAX_EXECUTION_TIME\((\d+)\)', sql)[1])
        self.limits.append(limit)
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
    assert caught.value.code == 3024 and repo.calls == [[42, 43], [42], [42]]
    assert repo.limits == [3000, 3000, 10000]
    assert table['last_single_read_retry']['recovered'] is False
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
        'phase': 'PAYLOAD', 'code': 3024, 'count': 4, 'first_seq': 0, 'last_seq': 3,
        'query_limit_ms': 3000}
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


def test_successful_slow_page_reduces_next_batch_before_timeout(monkeypatch):
    from sessionquant.database import retention
    ticks = iter([0., 2.5])
    monkeypatch.setattr(retention, 'monotonic', lambda: next(ticks))
    table = {}
    repo = Repo(size=10)
    assert _read_payload_page(repo, 'research_events', '1=1',
        [(i, None) for i in range(10)], table, 90, lambda: 0) == list(range(10))
    assert table['payload_batch_size'] == 3
    assert table['timings']['PAYLOAD']['timeouts'] == 0
    assert repo.limits == [3000]


def test_metadata_timeout_splits_without_skipping_or_reading_cached_bodies(monkeypatch):
    from sessionquant.database import retention_metadata as meta
    monkeypatch.setattr(meta, 'supports', lambda repo, kind: True)
    calls = []
    def read(repo, keys, condition):
        calls.append([key[0] for key in keys])
        if len(keys) > 2:
            raise MySQLRepositoryError(pymysql.err.OperationalError(3024, 'fixture'), 'SELECT')
        return [(key[0],) for key in keys]
    monkeypatch.setattr(meta, 'read', read)
    table = {}
    repo = Repo()
    assert _read_payload_page(repo, 'research_events', '1=1',
        [(i, None) for i in range(4)], table, 90, lambda: 0) == [(i,) for i in range(4)]
    assert calls == [[0, 1, 2, 3], [0, 1], [2, 3]]
    assert not repo.calls
    assert table['timings']['METADATA_READ']['timeouts'] == 1


@pytest.mark.parametrize('remaining,limit', [(90, 10000), (4.5, 4500)])
def test_single_record_retry_is_bounded_and_reads_the_same_key(remaining, limit):
    class SlowRecord(Repo):
        def _execute(self, sql, params, **kwargs):
            self.size = 0 if 'MAX_EXECUTION_TIME(3000)' in sql else 1
            return super()._execute(sql, params, **kwargs)
    repo, table = SlowRecord(), {}
    ticks = iter([0, 90-remaining])
    assert _read_payload_page(repo, 'research_events', '1=1', [(651777, None)],
                              table, 90, lambda: next(ticks)) == [651777]
    assert repo.calls == [[651777], [651777]] and repo.limits == [3000, limit]
    assert table['single_read_retries'] == 1
    assert table['last_single_read_retry'] == {
        'count': 1, 'first_seq': 651777, 'last_seq': 651777,
        'query_limit_ms': limit, 'recovered': True}


@pytest.mark.parametrize('remaining', [3, 1, 0, -1])
def test_single_retry_defers_when_overall_budget_is_insufficient(remaining):
    repo, table = Repo(size=0), {}
    ticks = iter([0, 90-remaining])
    with pytest.raises(TimeoutError):
        _read_payload_page(repo, 'research_events', '1=1', [(42, None)],
                           table, 90, lambda: next(ticks))
    assert repo.calls == [[42]] and 'single_read_retries' not in table
