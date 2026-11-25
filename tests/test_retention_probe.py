"""Bounded startup decisions, including uncertainty that must never skip work."""
from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import re
from types import SimpleNamespace

import pytest

from sessionquant.database.mysql_repository import MySQLRepositoryError
from sessionquant.database.retention import RetentionPolicy, retention_rules
from sessionquant.database.retention_probe import probe_retention, probe_with_engine_lease

NOW = datetime(2026, 11, 25, 15, tzinfo=timezone.utc)
POLICY = RetentionPolicy(enabled=True, intraday_auxiliary=True)


class ReadOnlyRepo:
    backend = "mysql"
    namespace = "paper_probe_fixture"
    config = SimpleNamespace(environment="PAPER_LOCAL")
    _in_transaction = False

    def __init__(self):
        self.rows = {}
        self.references = {"signals": [], "order_intents": []}
        self.state = []
        self.sql = []
        self.clock = 0
        self.after_read = None

    def add(self, kind, seq, *, days=2, allowed=1, metadata=None, stored_days=None):
        captured = (NOW - timedelta(days=days)).replace(tzinfo=None)
        stored = (NOW - timedelta(days=days if stored_days is None else stored_days)).replace(tzinfo=None)
        metadata = {"timestamp": captured.replace(tzinfo=timezone.utc).isoformat(), **(metadata or {})}
        row = (seq, captured, stored, stored, json.dumps(metadata), allowed)
        self.rows.setdefault(kind, []).append(row)

    def _execute(self, sql, params, *, fetch):
        assert sql.startswith("SELECT /*+ MAX_EXECUTION_TIME(")
        assert "COUNT(" not in sql and "SELECT *" not in sql
        assert params[0] == self.namespace and fetch == "all"
        self.sql.append(sql)
        kind = re.search(r"FROM pq_(\w+)", sql)[1]
        if kind == "controls":
            rows = self.state
        elif kind in self.references:
            after, limit = params[1:]
            rows = [row for row in self.references[kind] if row[0] > after][:limit]
        elif "created_at,updated_at" in sql:
            assert "FORCE INDEX (PRIMARY)" in sql
            rows = [row for row in self.rows.get(kind, []) if row[0] in params[1:]]
        else:
            assert "FORCE INDEX (ix_namespace_captured)" in sql
            before, limit = params[1], params[-1]
            rows = sorted([(row[0], row[1]) for row in self.rows.get(kind, []) if row[1] < before],
                          key=lambda row: (row[1], row[0]))
            if len(params) > 3:
                rows = [row for row in rows if (row[1], row[0]) > (params[2], params[4])]
            rows = rows[:limit]
        if self.after_read:
            rows = self.after_read(sql, rows)
        return rows


def test_empty_probe_reads_all_policy_scopes_without_protection_or_report_queries():
    repo = ReadOnlyRepo()
    report = probe_retention(repo, POLICY, now=NOW)
    assert report["status"] == "CLEAR" and report["read_only"]
    assert set(report["tables"]) == {row[0] for row in retention_rules(POLICY)}
    assert report["queries"] == len(report["tables"])
    assert report["scanned"] == report["protected_scanned"] == 0
    assert all(table["complete"] for table in report["tables"].values())


def test_expired_candidate_is_found_without_inspecting_entire_backlog():
    repo = ReadOnlyRepo()
    for seq in range(1000):
        repo.add("research_events", seq, metadata={"journal_run_id": "closed"})
    report = probe_retention(repo, POLICY, now=NOW)
    assert report["status"] == "NEEDS_CLEANUP" and report["table"] == "research_events"
    assert report["scanned"] == 10
    assert not report["tables"]["research_events"]["complete"]


def test_protected_pipeline_rows_do_not_force_cleanup_on_every_start():
    repo = ReadOnlyRepo()
    repo.references["order_intents"] = [(1, "used")]
    for seq in range(742):
        repo.add("pipeline_status", seq, allowed=seq % 2, metadata={"id": "used"})
    repo.add("research_events", 1000, metadata={"journal_run_id": "active"})
    repo.state = [("OBJECT", "true", '"active"')]
    report = probe_retention(repo, POLICY, now=NOW)
    assert report["status"] == "CLEAR" and report["scanned"] == 743
    assert report["tables"]["pipeline_status"]["complete"]
    assert max(len(re.findall("%s", sql)) for sql in repo.sql) <= 11


def test_current_day_collection_does_not_invalidate_no_expired_work_but_new_day_does():
    repo = ReadOnlyRepo()
    repo.add("quote_snapshots", 1, days=0)
    assert probe_retention(repo, POLICY, now=NOW)["status"] == "CLEAR"
    report = probe_retention(repo, POLICY, now=NOW + timedelta(days=1))
    assert report["status"] == "NEEDS_CLEANUP" and report["table"] == "quote_snapshots"


def test_rolling_cutoff_rechecks_on_second_update_same_calendar_day():
    repo = ReadOnlyRepo()
    repo.add("news_events", 1, days=30 - 1 / 24)
    assert probe_retention(repo, POLICY, now=NOW)["status"] == "CLEAR"
    assert probe_retention(repo, POLICY, now=NOW + timedelta(hours=2))["status"] == "NEEDS_CLEANUP"


@pytest.mark.parametrize("metadata,stored_days", [({"timestamp": "bad-date"}, 2),
                                                   ({}, 0), ({"timestamp": NOW.isoformat()}, 2)])
def test_expiry_guards_match_retention(metadata, stored_days):
    repo = ReadOnlyRepo()
    repo.add("quote_snapshots", 1, metadata=metadata, stored_days=stored_days)
    assert probe_retention(repo, POLICY, now=NOW)["status"] == "CLEAR"


def test_news_referenced_by_signal_is_preserved():
    repo = ReadOnlyRepo()
    repo.references["signals"] = [(1, "catalyst")]
    repo.add("news_events", 1, days=45, metadata={"id": "catalyst"})
    assert probe_retention(repo, POLICY, now=NOW)["status"] == "CLEAR"


def test_budget_exhaustion_is_unknown_even_if_checked_rows_are_all_protected():
    repo = ReadOnlyRepo()
    for seq in range(5):
        repo.add("pipeline_status", seq, allowed=0)
    report = probe_retention(repo, POLICY, now=NOW, max_rows=2)
    assert report["status"] == "UNKNOWN" and report["reason"] == "ROW_LIMIT"
    assert report["scanned"] == 2


def test_large_financial_reference_set_cannot_authorize_clear_from_partial_protections():
    repo = ReadOnlyRepo()
    repo.add("quote_snapshots", 1)
    repo.references["signals"] = [(1, "one"), (2, "two"), (3, "three")]
    report = probe_retention(repo, POLICY, now=NOW, max_rows=2)
    assert report["status"] == "UNKNOWN" and report["reason"] == "PROTECTION_ROW_LIMIT"
    assert report["scanned"] == 0


def test_query_crossing_deadline_never_proves_clear():
    repo = ReadOnlyRepo()
    def slow(sql, rows):
        repo.clock += 2
        return rows
    repo.after_read = slow
    report = probe_retention(repo, POLICY, now=NOW, max_seconds=1, clock=lambda: repo.clock)
    assert report["status"] == "UNKNOWN" and report["reason"] == "TIME_LIMIT"
    assert report["queries"] == 1


def test_query_error_is_safe_unknown_without_sql_or_credentials():
    repo = ReadOnlyRepo()
    def fail(sql, rows):
        raise MySQLRepositoryError(Exception(3024, "secret password and SQL"), "SELECT")
    repo.after_read = fail
    report = probe_retention(repo, POLICY, now=NOW)
    assert report["status"] == "UNKNOWN" and report["error"]["code"] == 3024
    assert "secret" not in json.dumps(report)


def test_row_disappearing_between_key_and_projection_read_is_unknown():
    repo = ReadOnlyRepo()
    repo.add("quote_snapshots", 1)
    repo.after_read = lambda sql, rows: [] if "created_at,updated_at" in sql else rows
    report = probe_retention(repo, POLICY, now=NOW)
    assert report["status"] == "UNKNOWN" and report["reason"] == "ROWS_CHANGED"


@pytest.mark.parametrize("state", [[("ARRAY", None, None)], [("OBJECT", '"yes"', '"run"')],
                                    [("OBJECT", "true", None)]])
def test_unknown_active_replay_state_never_proves_clear(state):
    repo = ReadOnlyRepo()
    repo.state = state
    repo.add("research_events", 1, metadata={"journal_run_id": "closed"})
    assert probe_retention(repo, POLICY, now=NOW)["status"] == "UNKNOWN"


def test_disabled_policy_does_not_query_or_change_database():
    repo = ReadOnlyRepo()
    assert probe_retention(repo, replace(POLICY, enabled=False), now=NOW)["status"] == "DISABLED"
    assert repo.sql == []


def test_command_opens_existing_schema_without_initialization(monkeypatch):
    from sessionquant.database import config as db_config, mysql_repository, retention_probe
    from sessionquant import session_config
    repo = ReadOnlyRepo()
    repo.close = lambda: None
    repo.leased = False
    @contextmanager
    def lease():
        repo.leased = True
        try:
            yield
        finally:
            repo.leased = False
    def require_lease(sql, rows):
        assert repo.leased
        return rows
    repo.engine_lease = lease
    repo.after_read = require_lease
    config = SimpleNamespace(database_backend="mysql", environment="PAPER_LOCAL", db_path="paper.db", retention=POLICY.__dict__)
    monkeypatch.setattr(session_config, "load_session_config", lambda path: config)
    monkeypatch.setattr(db_config.MySQLConfig, "from_env", lambda: db_config.MySQLConfig(password="fixture"))
    def connect(path, config, *, initialize):
        assert initialize is False and config.environment == "PAPER_LOCAL"
        assert config.read_timeout <= 5
        return repo
    monkeypatch.setattr(mysql_repository, "MySQLRepository", connect)
    assert retention_probe.probe_command("fixture.yaml")["status"] == "CLEAR"
    assert repo.leased is False


def test_busy_engine_cannot_return_clear_or_read_candidate_tables():
    repo = ReadOnlyRepo()
    @contextmanager
    def busy():
        raise RuntimeError("같은 MySQL 환경에서 다른 엔진이 실행 중입니다")
        yield
    repo.engine_lease = busy
    result = probe_with_engine_lease(repo, POLICY, now=NOW)
    assert result["status"] == "UNKNOWN" and result["reason"] == "ENGINE_LEASE_UNAVAILABLE"
    assert result["stage"] == "ENGINE_LEASE" and repo.sql == []


def test_probe_unexpected_failure_is_not_mislabeled_as_busy_and_releases_lease():
    repo = ReadOnlyRepo()
    repo.released = False
    @contextmanager
    def lease():
        try:
            yield
        finally:
            repo.released = True
    def broken(sql, rows):
        raise RuntimeError("unexpected probe defect")
    repo.engine_lease = lease
    repo.after_read = broken
    with pytest.raises(RuntimeError, match="unexpected probe defect"):
        probe_with_engine_lease(repo, POLICY, now=NOW)
    assert repo.released
