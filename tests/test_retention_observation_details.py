"""Optional measurements distinguish query failures from exhausted maintenance budgets."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json

import pymysql
import pytest

from sessionquant.database import retention
from sessionquant.database.mysql_repository import MySQLRepositoryError


NOW = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)
KIND = "quote_snapshots"


def observe(monkeypatch, *, fail_at=None, budget=90, after_query=None, baseline=False, max_rows=50000):
    monkeypatch.setattr(retention, "retention_rules", lambda policy: ((KIND, "diagnostics_days", "1=1"),))
    elapsed = [0]
    calls = []

    class Repo:
        namespace = "fixture"

        def _execute(self, sql, params, fetch):
            calls.append(sql)
            if len(calls) == fail_at:
                raise MySQLRepositoryError(pymysql.err.OperationalError(3024, "private query credentials"), "SELECT")
            if after_query is not None:
                elapsed[0] = after_query
            return []

    policy = retention.RetentionPolicy(max_rows=max_rows)
    report = {"policy": asdict(policy), "status": "PARTIAL", "scanned": 0, "deleted": 7,
              "errors": [], "tables": {KIND: {"deleted": 7}}}
    previous = {"tables": {KIND: {"observation": {"generation": {
        "high_water_seq": 1, "measured_at": (NOW - timedelta(minutes=1)).isoformat()}}}}} if baseline else {}
    retention._observe_retention(Repo(), report, previous, {"news": set(), "selection": set()},
                                {KIND: {"highest": 1, "new_min": 0, "exact": True}},
                                budget, lambda: elapsed[0], NOW)
    return report, calls


@pytest.mark.parametrize(("failed_query", "stage"), [(1, "HIGH_WATER"), (2, "BACKLOG"), (3, "GENERATION")])
def test_optional_query_timeout_identifies_stage_without_losing_committed_deletions(monkeypatch, failed_query, stage):
    report, calls = observe(monkeypatch, fail_at=failed_query, baseline=True)
    observation = report["tables"][KIND]["observation"]
    assert observation["status"] == "FAILED"
    assert observation["error"] == "MySQLRepositoryError"
    assert observation["error_details"] == {"stage": stage, "code": 3024, "query_limit_ms": 250}
    assert observation["generation"]["high_water_seq"] is None
    assert len(calls) == failed_query
    assert report["deleted"] == 7 and report["errors"] == []
    assert "private query" not in json.dumps(report)


@pytest.mark.parametrize(("budget", "max_rows", "reason"), [(0, 50000, "TIME_LIMIT"), (90, 1, "ROW_LIMIT")])
def test_no_budget_means_skipped_measurement_and_no_database_failure(monkeypatch, budget, max_rows, reason):
    report, calls = observe(monkeypatch, budget=budget, max_rows=max_rows)
    observation = report["tables"][KIND]["observation"]
    assert observation["status"] == "SKIPPED_BUDGET" and observation["budget_reason"] == reason
    assert observation["backlog_status"] == "UNKNOWN"
    assert "error" not in observation and calls == []


def test_budget_expiring_during_successful_query_is_not_a_mysql_timeout(monkeypatch):
    report, calls = observe(monkeypatch, after_query=90)
    observation = report["tables"][KIND]["observation"]
    assert observation["status"] == "BUDGET_EXHAUSTED"
    assert observation["budget_reason"] == "TIME_LIMIT"
    assert observation["error"] == "TimeoutError"
    assert observation["error_details"] == {"stage": "HIGH_WATER", "code": None, "query_limit_ms": 250}
    assert len(calls) == 1 and report["deleted"] == 7


def test_successful_measurement_keeps_existing_backlog_semantics(monkeypatch):
    report, calls = observe(monkeypatch, baseline=True)
    observation = report["tables"][KIND]["observation"]
    assert observation["status"] == "COMPLETE" and observation["backlog_status"] == "CLEAR"
    assert observation["backlog_exact"] is True and observation["eligible_rows_min"] == 0
    assert observation["generation"]["rows_min"] == 0
    assert "error" not in observation and len(calls) == 3
