"""Opt-in, disposable MySQL tests. No production namespaces or external APIs."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os

import pytest

from test_mysql_integration import mysql_repositories  # noqa: F401 - shared isolated fixture
from sessionquant.database.retention import CURSORS, REPORT, RetentionPolicy, prune_expired

pytestmark = pytest.mark.skipif(os.getenv("PQ_TEST_MYSQL") != "1", reason="requires opt-in real MySQL 8")
POLICY = RetentionPolicy(enabled=True, batch_size=2)


def seed(repo, table_name, key, now, *, days=45, stored_days=None, **fields):
    stamp = now - timedelta(days=days)
    data = {"id": key, "timestamp": stamp.isoformat(), **fields}
    repo.import_record(table_name, data, key)
    persisted = now - timedelta(days=days if stored_days is None else stored_days)
    repo._execute(f"UPDATE pq_{table_name} SET captured_at=%s,created_at=%s,updated_at=%s "
                  "WHERE namespace=%s AND record_id=%s",
                  (stamp.replace(tzinfo=None), persisted.replace(tzinfo=None), persisted.replace(tzinfo=None), repo.namespace, key))
    return data


def receipt(tmp_path, now):
    path = tmp_path / "receipt"
    path.write_text(f"UPLOADED {now.isoformat()} sessionquant-20261118T210500Z-fixture.tar.gz\n")
    return path


def test_retention_preview_and_apply_preserve_finance_rvol_replay_and_references(mysql_repositories, tmp_path):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    other = mysql_repositories(environment="PAPER_LOCAL")
    news = {"news_events": {"id": "catalyst"}, "news_analysis": {"news_id": "catalyst"},
            "macro_news": {"id": "catalyst"}, "macro_analysis": {"article_id": "catalyst"}}
    for kind, reference in news.items():
        seed(repo, kind, "old-" + kind, now)
        seed(repo, kind, "protected-" + kind, now, **reference)
        seed(repo, kind, "recent-" + kind, now, days=29)
        seed(other, kind, "other-" + kind, now)
    seed(repo, "signals", "entry-signal", now, news_id="catalyst")
    seed(repo, "order_intents", "intent", now, selection_id="used-selection")
    seed(repo, "pipeline_status", "used-selection", now, kind="portfolio_selection")
    seed(repo, "pipeline_status", "unused-selection", now, kind="portfolio_selection")
    seed(repo, "pipeline_status", "risk-evidence", now, kind="risk_decision")
    for kind in ("news_ingestions", "news_enrichments", "macro_context_snapshots", "market_regimes", "system_logs", "price_samples", "quote_snapshots"):
        seed(repo, kind, "old-" + kind, now)
        seed(repo, kind, "new-" + kind, now, days=6)
        seed(repo, kind, "reimported-" + kind, now, stored_days=0)
    seed(repo, "news_ingestions", "future-news", now, published_at=(now + timedelta(days=1)).isoformat())
    seed(repo, "news_ingestions", "unknown-date", now, received_at="unverified")
    seed(repo, "macro_context_analysis", "skipped", now, status="SKIPPED_PRECHECK", llm_requested=False, llm_call_ids=[])
    seed(repo, "macro_context_analysis", "paid", now, status="OK", llm_requested=True, llm_call_ids=["paid-call"])
    seed(repo, "research_events", "diagnostic-copy", now, kind="news_ingestions", data={"article_id": "unused"})
    seed(repo, "research_events", "new-copy", now, kind="news_ingestions", ingested_at=now.isoformat(), data={})
    seed(repo, "research_events", "trade-copy", now, kind="news_ingestions", data={"article_id": "catalyst"})
    for kind in ("recording_start", "engine_batch", "news_events", "news_analysis", "macro_analysis", "recording_stop"):
        seed(repo, "research_events", "replay-" + kind, now, kind=kind, data={"evidence": "keep"})
    protected = ("market_history_sessions", "market_bars", "llm_usage", "llm_calls", "orders", "executions", "positions",
                 "account_snapshots", "daily_performance", "economic_events", "market_reference_metrics", "universe_history")
    for kind in protected:
        seed(repo, kind, "keep-" + kind, now, symbol="TEST", qty="0.123456")
    repo.save_state({"recovery": "keep", "cash": "123.456789"})
    for name in ("news_first_available:hash", "analysis_cache:hash", "llm_credit", "research_recording_v1"):
        repo.set_control(name, {"original": "keep"})
    before = {kind: repo.all(kind) for kind in repo.kinds}
    controls = repo._execute("SELECT name,payload FROM pq_controls WHERE namespace=%s ORDER BY name", (repo.namespace,), fetch="all")
    report = prune_expired(repo, POLICY, now=now)
    assert report["status"] == "COMPLETE" and report["deleted"] == 0 and report["eligible"] == 14
    assert before == {kind: repo.all(kind) for kind in repo.kinds}
    assert controls == repo._execute("SELECT name,payload FROM pq_controls WHERE namespace=%s ORDER BY name", (repo.namespace,), fetch="all")
    report = prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert report["status"] == "COMPLETE" and report["deleted"] == 14
    for kind in protected:
        assert repo.all(kind) == before[kind]
    assert repo.all("signals") == before["signals"] and repo.all("order_intents") == before["order_intents"]
    assert repo.load_state()["cash"] == "123.456789"
    assert repo.get_control("llm_credit") == {"original": "keep"}
    for kind in news:
        assert len(repo.all(kind)) == 2 and len(other.all(kind)) == 1
    assert {r["id"] for r in repo.all("pipeline_status")} == {"used-selection", "risk-evidence"}
    assert len(repo.all("research_events")) == 8
    assert repo.get_control(REPORT)["deleted"] == 14
    assert repo.get_control(CURSORS)["positions"] == {}
    assert prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))["deleted"] == 0


def test_apply_requires_backup_and_exclusive_engine_lease(mysql_repositories, tmp_path):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    seed(repo, "news_events", "expired", now)
    with pytest.raises(ValueError, match="백업"):
        prune_expired(repo, POLICY, now=now, apply=True)
    owner = mysql_repositories(repo.config.namespace, environment="PAPER_LOCAL")
    with owner.engine_lease(), pytest.raises(RuntimeError, match="다른 엔진"):
        prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert len(repo.all("news_events")) == 1 and repo.get_control(REPORT) is None
    live = mysql_repositories(environment="LIVE_LOCAL")
    with pytest.raises(ValueError, match="PAPER_LOCAL"):
        prune_expired(live, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))


def test_imported_timestamp_protects_records_after_persistence_dates_expire(mysql_repositories, tmp_path):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=45)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    for key, timestamp in (("expired", old.isoformat()), ("recent", (now - timedelta(days=1)).isoformat()),
                           ("future", (now + timedelta(days=1)).isoformat()), ("malformed", "unverified")):
        repo.import_record("quote_snapshots", {"id": key, "received_at": old.isoformat(), "timestamp": timestamp}, key)
        captured = repo._execute("SELECT captured_at FROM pq_quote_snapshots WHERE namespace=%s AND record_id=%s",
                                 (repo.namespace, key), fetch="one")[0]
        assert captured == old.replace(tzinfo=None)
        # Model restored records that have aged in storage; leave the actual
        # import projection intact so captured_at cannot mask the payload guard.
        repo._execute("UPDATE pq_quote_snapshots SET created_at=%s,updated_at=%s WHERE namespace=%s AND record_id=%s",
                      (old.replace(tzinfo=None), old.replace(tzinfo=None), repo.namespace, key))
    preview = prune_expired(repo, POLICY, now=now)
    assert preview["eligible"] == 1 and preview["deleted"] == 0
    report = prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert report["deleted"] == 1
    assert {row["id"] for row in repo.all("quote_snapshots")} == {"recent", "future", "malformed"}


def test_partial_cleanup_resumes_and_rotates_tables_even_at_one_row_budget(mysql_repositories, tmp_path):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    for key in ("a", "b", "c"):
        seed(repo, "news_ingestions", key, now)
    seed(repo, "news_events", "last-table", now)
    policy = replace(POLICY, batch_size=1, max_rows=1)
    proof = receipt(tmp_path, now)
    reports = [prune_expired(repo, policy, now=now, apply=True, receipt=proof) for _ in range(5)]
    assert all(report["scanned"] <= 1 and report["deleted"] <= 1 for report in reports)
    assert sum(report["deleted"] for report in reports) == 4
    assert not repo.all("news_events") and not repo.all("news_ingestions")
    assert reports[-1]["status"] == "COMPLETE"
    assert all(report["inspected"] <= 1 for report in reports)
    assert reports[0]["observability"]["backlog_status"] == "UNKNOWN"
    assert reports[0]["observability"]["eligible_backlog_min"] is None
    assert reports[0]["observability"]["stop_reason"] == "ROW_LIMIT"
    assert reports[1]["observability"]["attention"] == "CAPACITY_REVIEW"


def test_elapsed_budget_stops_before_delete_and_disabled_policy_is_inert(mysql_repositories, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    seed(repo, "news_ingestions", "expired", now)
    clock = [0]
    original = repo._execute
    def slow_query(sql, *args, **kwargs):
        result = original(sql, *args, **kwargs)
        if "JSON_OBJECT" in sql:
            clock[0] = 100
        return result
    monkeypatch.setattr(repo, "_execute", slow_query)
    report = prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now), clock=lambda: clock[0])
    assert report["status"] == "PARTIAL" and report["deleted"] == 0
    assert report["observability"]["backlog_status"] == "UNKNOWN"
    assert report["observability"]["observation_scanned"] == 0
    assert report["observability"]["stop_reason"] == "TIME_LIMIT"
    assert len(repo.all("news_ingestions")) == 1
    assert prune_expired(repo, replace(POLICY, enabled=False), apply=True)["status"] == "DISABLED"


def test_checkpoint_failure_can_repeat_safely_without_skipping_rows(mysql_repositories, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    for key in ("a", "b", "c"):
        seed(repo, "news_ingestions", key, now)
    proof = receipt(tmp_path, now)
    setter = repo.set_control
    def failed_checkpoint(name, payload):
        if name == CURSORS:
            raise RuntimeError("fixture checkpoint failure")
        setter(name, payload)
    monkeypatch.setattr(repo, "set_control", failed_checkpoint)
    with pytest.raises(RuntimeError, match="checkpoint"):
        prune_expired(repo, POLICY, now=now, apply=True, receipt=proof)
    assert len(repo.all("news_ingestions")) == 1 and repo.get_control(CURSORS) is None
    monkeypatch.setattr(repo, "set_control", setter)
    report = prune_expired(repo, POLICY, now=now, apply=True, receipt=proof)
    assert report["status"] == "COMPLETE" and report["deleted"] == 1
    assert not repo.all("news_ingestions")


def test_observations_measure_actual_namespace_arrivals_without_warning_on_young_rows(mysql_repositories, tmp_path):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    other = mysql_repositories(environment="PAPER_LOCAL")
    first = prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert first["tables"]["quote_snapshots"]["observation"]["generation"]["rows_min"] is None
    assert first["observability"]["backlog_status"] == "CLEAR"
    for index in range(12):
        other.import_record("quote_snapshots", {"id": str(index), "timestamp": now}, str(index))
    for index in range(2):
        repo.import_record("quote_snapshots", {"id": str(index), "timestamp": now}, str(index))
    later = now + timedelta(seconds=60)
    report = prune_expired(repo, POLICY, now=later, apply=True, receipt=receipt(tmp_path, later))
    measured = report["tables"]["quote_snapshots"]["observation"]["generation"]
    assert measured["rows_min"] == 2 and measured["exact"] is True
    assert measured["interval_seconds"] == 60
    assert report["deleted"] == 0 and report["observability"]["attention"] == "CLEAR"
    assert report["inspected"] <= POLICY.max_rows


def test_arrival_sample_is_a_lower_bound_when_capped(mysql_repositories, tmp_path):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))
    for index in range(100):
        repo.import_record("quote_snapshots", {"id": str(index), "timestamp": now}, str(index))
    later = now + timedelta(seconds=120)
    report = prune_expired(repo, POLICY, now=later, apply=True, receipt=receipt(tmp_path, later))
    measured = report["tables"]["quote_snapshots"]["observation"]["generation"]
    assert 0 < measured["rows_min"] < 100 and measured["exact"] is False
    assert measured["interval_seconds"] == 120
    assert report["observability"]["attention"] == "CLEAR"


def skip_old_quotes(repo, now):
    repo.set_control(CURSORS, {"policy": {"version": 1, "diagnostics_days": 7, "news_days": 30},
                              "positions": {"quote_snapshots": [(now - timedelta(days=20)).isoformat(), 1]}})


def test_complete_cursor_pass_does_not_claim_older_backfill_is_gone(mysql_repositories, tmp_path):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    seed(repo, "quote_snapshots", "older-backfill", now)
    skip_old_quotes(repo, now)
    report = prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert report["status"] == "COMPLETE" and report["deleted"] == 0
    observation = report["tables"]["quote_snapshots"]["observation"]
    assert observation["backlog_status"] == "PRESENT" and observation["eligible_rows_min"] == 1
    assert observation["backlog_exact"] is True and observation["oldest_eligible_age_days"] == 45
    assert report["observability"]["attention"] == "BACKLOG"


def test_protected_sample_prefix_is_unknown_instead_of_zero_backlog(mysql_repositories, tmp_path):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    seed(repo, "signals", "signal", now, news_id="keep")
    for index in range(128):
        seed(repo, "quote_snapshots", str(index), now, news_id="keep")
    seed(repo, "quote_snapshots", "unseen-expired", now, days=44)
    skip_old_quotes(repo, now)
    report = prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))
    observation = report["tables"]["quote_snapshots"]["observation"]
    assert report["status"] == "COMPLETE" and report["deleted"] == 0
    assert observation["backlog_status"] == "UNKNOWN" and observation["eligible_rows_min"] is None
    assert report["observability"]["backlog_status"] == "UNKNOWN"
    assert report["observability"]["eligible_backlog_min"] is None
    assert len(repo.all("quote_snapshots")) == 129


def test_arrivals_include_new_rows_deleted_during_this_cleanup(mysql_repositories, tmp_path):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))
    later = now + timedelta(seconds=60)
    seed(repo, "quote_snapshots", "imported-old", later)
    report = prune_expired(repo, POLICY, now=later, apply=True, receipt=receipt(tmp_path, later))
    measured = report["tables"]["quote_snapshots"]["observation"]["generation"]
    assert measured["rows_min"] == 1 and measured["exact"] is True
    assert measured["high_water_seq"] > 0 and report["deleted"] == 1


def test_observation_failure_keeps_committed_cleanup_and_reports_unknown(mysql_repositories, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    seed(repo, "quote_snapshots", "expired", now)
    original = repo._execute
    def failed_observation(sql, *args, **kwargs):
        if "MAX_EXECUTION_TIME(250)" in sql:
            raise RuntimeError("fixture observation failure")
        return original(sql, *args, **kwargs)
    monkeypatch.setattr(repo, "_execute", failed_observation)
    report = prune_expired(repo, POLICY, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert report["deleted"] == 1 and not repo.all("quote_snapshots")
    assert report["status"] == "COMPLETE" and report["observability"]["backlog_status"] == "UNKNOWN"
    assert repo.get_control(REPORT)["deleted"] == 1
