"""Cold replay payloads must not turn optional growth statistics into cleanup failure."""
from dataclasses import asdict
from datetime import timedelta
import json

import pymysql
import pytest

from sessionquant.database import retention
from sessionquant.database.mysql_repository import MySQLRepositoryError
from test_retention_observation_details import NOW


def observe(monkeypatch, *, count=12, batch_capacity=2, code=3024,
            condition="payload->>'$.kind'='engine_batch'", expire=False, missing=False):
    kind = 'research_events'
    monkeypatch.setattr(retention, 'retention_rules', lambda policy: ((kind, 'diagnostics_days', condition),))
    calls, elapsed = [], [0]

    class Repo:
        namespace = 'fixture'

        def _execute(self, sql, params, *, fetch, optional_observation):
            assert optional_observation and 'MAX_EXECUTION_TIME(250)' in sql
            assert params[0] == self.namespace
            calls.append((sql, params))
            if 'ORDER BY seq DESC' in sql:
                return [(1000,)]
            if 'captured_at<' in sql:
                return []
            if 'seq>%s' in sql:
                assert 'payload' not in sql  # covering index lookup, no JSON reads
                return [(i,) for i in range(2, 2 + min(count, params[-1]))]
            assert 'FORCE INDEX (PRIMARY)' in sql
            ids = params[1:]
            if len(ids) > batch_capacity:
                if expire:
                    elapsed[0] = 91
                raise MySQLRepositoryError(pymysql.err.OperationalError(code, 'PRIVATE payload'), 'SELECT')
            return [(i, i % 2 == 0) for i in ids if not missing or i != 2]

    report = {'policy': asdict(retention.RetentionPolicy()), 'status': 'COMPLETE',
              'scanned': 0, 'deleted': 2, 'errors': [], 'tables': {kind: {'deleted': 2}}}
    previous = {'tables': {kind: {'observation': {'generation': {
        'high_water_seq': 1, 'measured_at': (NOW - timedelta(seconds=60)).isoformat()}}}}}
    retention._observe_retention(Repo(), report, previous, {'news': set(), 'selection': set()},
        {kind: {'highest': 1, 'new_min': 2, 'exact': True}}, 90, lambda: elapsed[0], NOW)
    return report, report['tables'][kind]['observation'], calls


def test_split_payloads_count_each_matching_row_once_and_keep_both_budgets(monkeypatch):
    report, obs, calls = observe(monkeypatch)
    assert obs['generation']['rows_min'] == 8  # six matching survivors + two deleted
    assert obs['generation']['exact'] is True
    assert obs['generation']['interval_seconds'] == 60
    assert obs['status'] == 'COMPLETE' and obs['generation_read_splits'] == 2
    assert obs['timings']['GENERATION_PAYLOAD']['timeouts'] == 2
    assert report['observability']['observation_scanned'] == 25  # endpoint + keys + payloads
    assert all(len(params) - 1 <= 10 for sql, params in calls if 'FORCE INDEX (PRIMARY)' in sql)
    assert 'PRIVATE' not in json.dumps(report)


def test_unconditional_table_counts_keys_without_loading_payloads(monkeypatch):
    _, obs, calls = observe(monkeypatch, condition='1=1')
    assert obs['generation']['rows_min'] == 14 and obs['generation']['exact']
    assert len(calls) == 3


@pytest.mark.parametrize('code,capacity,stage', [(3024, 0, 'GENERATION_PAYLOAD'), (2013, 0, 'GENERATION_PAYLOAD')])
def test_unreadable_payload_keeps_cleanup_and_backlog_but_growth_unknown(monkeypatch, code, capacity, stage):
    report, obs, calls = observe(monkeypatch, batch_capacity=capacity, code=code)
    assert report['status'] == 'COMPLETE' and report['deleted'] == 2 and report['errors'] == []
    assert obs['backlog_status'] == 'CLEAR' and obs['eligible_rows_min'] == 0
    assert obs['generation']['rows_min'] is None and obs['status'] == 'FAILED'
    assert obs['error_details'] == {'stage': stage, 'code': code, 'query_limit_ms': 250}
    if code == 2013:
        assert len(calls) == 4 and 'generation_read_splits' not in obs
    else:
        assert obs['read_batch']['count'] == 1 and len(calls) == 7


def test_retry_stops_when_global_budget_expires(monkeypatch):
    _, obs, calls = observe(monkeypatch, expire=True)
    assert obs['status'] == 'BUDGET_EXHAUSTED' and obs['budget_reason'] == 'TIME_LIMIT'
    assert obs['backlog_status'] == 'CLEAR' and len(calls) == 4


@pytest.mark.parametrize('options', [{'count': 100}, {'missing': True}])
def test_capped_or_changed_samples_are_never_exact(monkeypatch, options):
    report, obs, _ = observe(monkeypatch, **options)
    assert obs['generation']['rows_min'] > 0 and obs['generation']['exact'] is False
    assert report['observability']['observation_scanned'] <= 128
