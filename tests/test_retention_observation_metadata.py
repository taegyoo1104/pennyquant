"""Optional observations reuse checked metadata without weakening their budgets."""
from dataclasses import asdict
from datetime import timedelta

import pymysql
import pytest

from sessionquant.database import retention, retention_metadata
from sessionquant.database.mysql_repository import MySQLRepositoryError
from test_retention_observation_details import NOW


@pytest.mark.parametrize('failure', ['timeout', 'changed', 'budget'])
def test_incomplete_metadata_observation_never_reports_empty_backlog(monkeypatch, failure):
    monkeypatch.setattr(retention, 'retention_rules', lambda _: (
        ('research_events', 'diagnostics_days', "payload->>'$.kind'='engine_batch'"),))
    monkeypatch.setattr(retention_metadata, 'supports', lambda *_: True)
    stamp = (NOW - timedelta(days=8)).replace(tzinfo=None)
    elapsed, calls = [0], []

    class Repo:
        namespace = 'fixture'

        def _execute(self, sql, params, *, fetch, optional_observation):
            assert optional_observation and 'MAX_EXECUTION_TIME(250)' in sql
            assert sql.startswith('SELECT') and params[0] == self.namespace
            calls.append(sql)
            if 'ORDER BY seq DESC' in sql:
                return [(1,)]
            if 'ix_namespace_captured' in sql:
                assert 'payload' not in sql
                return [(1, stamp)]
            if 'STRAIGHT_JOIN' in sql:
                assert 'r.payload' not in sql
                if failure == 'timeout':
                    raise MySQLRepositoryError(pymysql.err.OperationalError(3024, 'private'), 'SELECT')
                if failure == 'budget':
                    elapsed[0] = 91
                return []
            assert failure == 'changed' and 'FORCE INDEX (PRIMARY)' in sql
            return []  # deleted/changed source is not proof of an empty sample

    report = {'policy': asdict(retention.RetentionPolicy()), 'status': 'COMPLETE',
              'scanned': 0, 'deleted': 2, 'errors': [], 'tables': {'research_events': {'deleted': 2}}}
    retention._observe_retention(Repo(), report, {}, {'news': set(), 'selection': set()},
        {'research_events': {'highest': 0, 'new_min': 0, 'exact': True}}, 90, lambda: elapsed[0], NOW)
    obs = report['tables']['research_events']['observation']
    assert obs['backlog_status'] == 'UNKNOWN' and obs['eligible_rows_min'] is None
    assert obs['status'] == ('BUDGET_EXHAUSTED' if failure == 'budget' else 'FAILED')
    assert report['status'] == 'COMPLETE' and report['deleted'] == 2 and report['errors'] == []
    assert len(calls) == (4 if failure == 'changed' else 3)

