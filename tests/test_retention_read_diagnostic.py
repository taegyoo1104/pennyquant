from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import pytest

from sessionquant.database.retention import CURSORS, REPORT, RetentionPolicy
from dataclasses import asdict

SPEC = importlib.util.spec_from_file_location('retention_probe', Path(__file__).parents[1] / 'scripts/diagnose_retention_read.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
STAMP = datetime(2026, 11, 20, tzinfo=timezone.utc)


class Timeout(Exception):
    code = 3024


def state():
    policy = asdict(RetentionPolicy(enabled=True, intraday_auxiliary=True))
    return [(REPORT, {'namespace': 'paper_fixture', 'policy': policy,
                     'errors': [{'table': 'pq_research_events', 'code': 3024}],
                     'tables': {'research_events': {'before': STAMP.isoformat()}}}),
            (CURSORS, {'policy': {'version': 2, 'diagnostics_days': 7, 'news_days': 30, 'intraday_auxiliary': True},
                       'positions': {'research_events': [STAMP.isoformat(), 42]}})]


class Repo:
    namespace = 'paper_fixture'

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def _execute(self, sql, params=(), **kwargs):
        self.calls.append((sql, params))
        assert sql.startswith(('SET SESSION ', 'SELECT ', 'SHOW ', 'EXPLAIN '))
        if 'FROM pq_controls' in sql:
            return state()
        if sql.startswith('EXPLAIN'):
            return []
        if 'ORDER BY captured_at,seq LIMIT' in sql:
            assert params[-1] == 200 and params[-2] == 42
            if self.fail == 'keys':
                raise Timeout('private query')
            return [(43 + i, STAMP.replace(tzinfo=None)) for i in range(23)]
        if 'JSON_OBJECT' in sql and self.fail == 'payload':
            raise Timeout('private payload')
        return []


@pytest.mark.parametrize('phase,stage', [('keys', 'candidate_keys'), ('payload', 'payload_page_1')])
def test_reports_exact_timed_out_stage_without_writing_or_exposing_contents(phase, stage):
    repo = Repo(phase)
    result = probe.diagnose(repo)
    assert repo.calls[0][0] == 'SET SESSION TRANSACTION READ ONLY'
    assert result['status'] == 'PROBE_STOPPED' and result['code'] == 3024
    assert [q['stage'] for q in result['queries'] if q.get('code') == 3024] == [stage]
    assert 'private' not in str(result)
    assert ('failed_ids' in result) == (phase == 'payload')


def test_success_is_only_non_reproduction_and_each_payload_probe_is_at_most_ten_keys():
    repo = Repo()
    result = probe.diagnose(repo)
    assert result['status'] == 'NOT_REPRODUCED_IN_PAGE'
    assert [len(p)-1 for s, p in repo.calls if s.startswith('SELECT') and 'JSON_OBJECT' in s] == [10, 10, 3]
    assert result['candidate_count'] == 23


def test_overall_budget_stops_before_next_query():
    times = iter([0, 61, 61])
    repo = Repo()
    result = probe.diagnose(repo, clock=lambda: next(times))
    assert result['error'] == 'TimeoutError' and not result['queries']
    assert all(sql.startswith('SET SESSION') for sql, _ in repo.calls)


def test_missing_failure_record_does_not_scan_research_table():
    class Missing(Repo):
        def _execute(self, sql, params=(), **kwargs):
            rows = super()._execute(sql, params, **kwargs)
            return [] if 'FROM pq_controls' in sql else rows
    repo = Missing()
    assert probe.diagnose(repo)['error'] == 'ValueError'
    assert not any('FROM pq_research_events' in sql for sql, _ in repo.calls)
