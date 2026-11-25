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


def test_startup_probe_is_read_only_and_rechecks_after_cleanup_without_repeating_it(mysql_repositories, tmp_path, monkeypatch):
    from sessionquant.database.retention_probe import probe_retention
    now = datetime(2026, 11, 25, 15, tzinfo=timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    other = mysql_repositories(environment="PAPER_LOCAL")
    policy = replace(POLICY, intraday_auxiliary=True)
    seed(repo, "research_events", "expired", now, days=1, kind="engine_batch", run_id="closed", data={})
    seed(other, "research_events", "other", now, days=1, kind="engine_batch", run_id="other", data={})
    seed(repo, "pipeline_status", "used-selection", now, days=1, kind="portfolio_selection")
    seed(repo, "pipeline_status", "risk-evidence", now, days=1, kind="risk_decision")
    seed(repo, "order_intents", "intent", now, days=1, selection_id="used-selection")
    seed(repo, "signals", "signal", now, news_id="used-news")
    seed(repo, "news_events", "used-news", now)
    seed(repo, "research_events", "active", now, days=1, kind="engine_batch", run_id="active", data={})
    repo.set_control("research_recording_v1", {"active": True, "run_id": "active"})
    execute = repo._execute
    queries = []
    def read_only(sql, *args, **kwargs):
        assert sql.startswith("SELECT") and "COUNT(" not in sql
        queries.append(sql)
        return execute(sql, *args, **kwargs)
    monkeypatch.setattr(repo, "_execute", read_only)
    assert probe_retention(repo, policy, now=now)["status"] == "NEEDS_CLEANUP"
    assert repo.get_control(REPORT) is None
    assert any("FORCE INDEX (PRIMARY)" in sql for sql in queries)
    monkeypatch.setattr(repo, "_execute", execute)
    assert prune_expired(repo, policy, now=now, apply=True, receipt=receipt(tmp_path, now))["deleted"] == 1
    # A second update on the same trading day sees newly collected data, but
    # skips cleanup because only protected/retained rows remain eligible by age.
    seed(repo, "quote_snapshots", "today", now, days=0)
    seed(repo, "research_events", "today", now, days=0, kind="engine_batch", run_id="today", data={})
    report_before = repo.get_control(REPORT)
    cursor_before = repo.get_control(CURSORS)
    monkeypatch.setattr(repo, "_execute", read_only)
    result = probe_retention(repo, policy, now=now + timedelta(minutes=1))
    assert result["status"] == "CLEAR"
    assert repo.get_control(REPORT) == report_before and repo.get_control(CURSORS) == cursor_before
    assert len(other.all("research_events")) == 1
    assert result["tables"]["pipeline_status"]["scanned"] == 2


def test_startup_probe_covers_all_retention_rule_scopes_and_uses_real_predicates(mysql_repositories):
    from sessionquant.database.retention import retention_rules
    from sessionquant.database.retention_probe import probe_retention
    now = datetime(2026, 11, 25, 15, tzinfo=timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    policy = replace(POLICY, intraday_auxiliary=True)
    for kind, _, _ in retention_rules(policy):
        fields = {}
        if kind == "macro_context_analysis":
            fields = {"status": "SKIPPED_PRECHECK", "llm_requested": False, "llm_call_ids": []}
        elif kind == "pipeline_status":
            fields = {"kind": "portfolio_selection"}
        elif kind == "research_events":
            fields = {"kind": "engine_batch", "run_id": "old", "data": {}}
        seed(repo, kind, "eligible", now, **fields)
        result = probe_retention(repo, policy, now=now)
        assert result["status"] == "NEEDS_CLEANUP" and result["table"] == kind
        repo._execute(f"DELETE FROM pq_{kind} WHERE namespace=%s", (repo.namespace,))
    assert probe_retention(repo, policy, now=now)["status"] == "CLEAR"


def test_startup_probe_does_not_confuse_read_timeout_with_no_cleanup(mysql_repositories, monkeypatch):
    from sessionquant.database.mysql_repository import MySQLRepositoryError
    from sessionquant.database.retention_probe import probe_retention
    repo = mysql_repositories(environment="PAPER_LOCAL")
    execute = repo._execute
    def timeout(sql, *args, **kwargs):
        if "FROM pq_research_events" in sql:
            raise MySQLRepositoryError(Exception(3024, "fixture timeout"), "SELECT")
        return execute(sql, *args, **kwargs)
    monkeypatch.setattr(repo, "_execute", timeout)
    result = probe_retention(repo, replace(POLICY, intraday_auxiliary=True))
    assert result["status"] == "UNKNOWN" and result["error"]["code"] == 3024
    monkeypatch.setattr(repo, "_execute", execute)


def test_startup_probe_engine_lease_blocks_external_writer_and_is_released(mysql_repositories):
    from sessionquant.database.retention_probe import probe_with_engine_lease
    owner = mysql_repositories(environment="PAPER_LOCAL")
    reader = mysql_repositories(owner.config.namespace, environment="PAPER_LOCAL")
    policy = replace(POLICY, intraday_auxiliary=True)
    with owner.engine_lease():
        result = probe_with_engine_lease(reader, policy)
        assert result["status"] == "UNKNOWN" and result["reason"] == "ENGINE_LEASE_UNAVAILABLE"
        assert result["scanned"] == 0 and result["tables"] == {}
    assert probe_with_engine_lease(reader, policy)["status"] == "CLEAR"
    assert reader._lease_depth == 0
    # Backup/apply must acquire independently after the read-only probe exits.
    with owner.engine_lease():
        assert owner._lease_depth == 1


def test_intraday_retention_expires_replay_but_keeps_today_active_and_financial_evidence(mysql_repositories, tmp_path):
    from sessionquant.database.retention import REPLAY_PRUNED
    now = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    other = mysql_repositories(environment="PAPER_LOCAL")
    for kind in ('recording_start', 'engine_batch', 'watchlist_snapshots_v1', 'recording_stop'):
        seed(repo, 'research_events', kind, now, days=1, kind=kind, run_id='yesterday', data={})
    seed(other, 'research_events', 'other-run', now, days=1, kind='engine_batch', run_id='other', data={})
    seed(repo, 'research_events', 'today', now, days=0, kind='engine_batch', run_id='today', data={})
    seed(repo, 'research_events', 'active', now, days=1, kind='engine_batch', run_id='active', data={})
    seed(repo, 'research_events', 'new-import', now, days=1, stored_days=0, kind='engine_batch', run_id='new-import', data={})
    seed(repo, 'research_events', 'bad-time', now, days=1, kind='engine_batch', run_id='bad-time', ingested_at='unknown', data={})
    seed(repo, 'research_events', 'bad-run', now, days=1, kind='engine_batch', data={})
    seed(repo, 'research_events', 'unknown-kind', now, days=1, kind='new-unrecognized-kind', run_id='unknown', data={})
    repo.set_control('research_recording_v1', {'active': True, 'run_id': 'active'})
    seed(repo, 'quote_snapshots', 'old-quote', now, days=1)
    seed(repo, 'quote_snapshots', 'today-quote', now, days=0)
    seed(repo, 'pipeline_status', 'old-focus', now, days=1, kind='watchlist_focus')
    seed(repo, 'pipeline_status', 'old-selection', now, days=1, kind='portfolio_selection')
    seed(repo, 'pipeline_status', 'used-selection', now, days=1, kind='portfolio_selection')
    seed(repo, 'pipeline_status', 'risk-evidence', now, days=1, kind='risk_decision')
    seed(repo, 'order_intents', 'intent', now, days=1, selection_id='used-selection')
    keep = ('orders', 'executions', 'positions', 'account_snapshots', 'daily_performance', 'market_bars', 'market_history_sessions', 'llm_usage', 'news_events', 'system_logs', 'market_regimes')
    for kind in keep:
        seed(repo, kind, 'keep-'+kind, now, days=1)
    repo.save_state({'cash': '123.45', 'positions': {'TEST': '0.01'}})
    before = {kind: repo.all(kind) for kind in keep}
    policy = replace(POLICY, intraday_auxiliary=True)
    preview = prune_expired(repo, policy, now=now)
    assert preview['eligible'] == 7 and preview['deleted'] == 0
    assert repo.get_control(REPLAY_PRUNED) is None
    report = prune_expired(repo, policy, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert report['deleted'] == 7
    assert repo.get_control(REPLAY_PRUNED)['runs'] == {'yesterday': {'before': '2026-11-20T05:00:00+00:00', 'backup': report['backup']}}
    assert {row['id'] for row in repo.all('research_events')} == {'today', 'active', 'new-import', 'bad-time', 'bad-run', 'unknown-kind'}
    assert len(other.all('research_events')) == 1
    assert {row['id'] for row in repo.all('pipeline_status')} == {'used-selection', 'risk-evidence'}
    assert {kind: repo.all(kind) for kind in keep} == before
    assert repo.load_state()['cash'] == '123.45'
    assert repo.get_control('research_recording_v1')['run_id'] == 'active'


def test_postclose_retention_archives_finished_day_and_keeps_first_recovery_backup(mysql_repositories, tmp_path, monkeypatch):
    from sessionquant.database.retention import REPLAY_PRUNED
    now = datetime(2026, 11, 20, 21, 5, tzinfo=timezone.utc)
    repo = mysql_repositories(environment='PAPER_LOCAL')
    seed(repo, 'research_events', 'closed-batch', now, days=1/24, kind='engine_batch', run_id='closed', data={})
    assert prune_expired(repo, POLICY, now=now)['eligible'] == 0  # opt-in only
    policy = replace(POLICY, intraday_auxiliary=True)
    report = prune_expired(repo, policy, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert report['deleted'] == 1
    first = repo.get_control(REPLAY_PRUNED)
    marker_writes = []
    original = repo.set_control
    def track(name, value):
        if name == REPLAY_PRUNED:
            marker_writes.append(value)
        original(name, value)
    monkeypatch.setattr(repo, 'set_control', track)
    seed(repo, 'research_events', 'later-import', now, days=1/24, kind='engine_batch', run_id='closed', data={})
    proof = tmp_path/'new-receipt'
    proof.write_text(f'UPLOADED {now.isoformat()} sessionquant-20261120T210500Z-second.tar.gz\n')
    assert prune_expired(repo, policy, now=now, apply=True, receipt=proof)['deleted'] == 1
    assert repo.get_control(REPLAY_PRUNED) == first
    assert marker_writes == []
    seed(repo, 'research_events', 'another-old-run-row', now, days=1/24, kind='engine_batch', run_id='closed', data={})
    seed(repo, 'research_events', 'new-run-row', now, days=1/24, kind='engine_batch', run_id='new-run', data={})
    assert prune_expired(repo, policy, now=now, apply=True, receipt=proof)['deleted'] == 2
    assert len(marker_writes) == 1
    marker = repo.get_control(REPLAY_PRUNED)
    assert marker['runs']['closed'] == first['runs']['closed']
    assert marker['runs']['new-run']['backup'] == 'sessionquant-20261120T210500Z-second.tar.gz'

    # A new marker must still commit atomically with its data deletion, even
    # when prior runs are already marked and need no further writes.
    seed(repo, 'research_events', 'failed-new-run-row', now, days=1/24, kind='engine_batch', run_id='failed-run', data={})
    def fail(name, value):
        original(name, value)
        if name == REPLAY_PRUNED:
            raise RuntimeError('fixture after marker write')
    monkeypatch.setattr(repo, 'set_control', fail)
    with pytest.raises(RuntimeError, match='fixture after marker write'):
        prune_expired(repo, policy, now=now, apply=True, receipt=proof)
    assert [row['id'] for row in repo.all('research_events')] == ['failed-new-run-row']
    assert repo.get_control(REPLAY_PRUNED) == marker


def test_failed_replay_tombstone_rolls_back_data_deletion(mysql_repositories, tmp_path, monkeypatch):
    from sessionquant.database.retention import REPLAY_PRUNED
    now = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)
    repo = mysql_repositories(environment='PAPER_LOCAL')
    seed(repo, 'research_events', 'old', now, days=1, kind='engine_batch', run_id='old', data={})
    original = repo.set_control
    def fail(name, payload):
        if name == REPLAY_PRUNED:
            raise RuntimeError('fixture marker failure')
        original(name, payload)
    monkeypatch.setattr(repo, 'set_control', fail)
    with pytest.raises(RuntimeError, match='fixture marker failure'):
        prune_expired(repo, replace(POLICY, intraday_auxiliary=True), now=now, apply=True, receipt=receipt(tmp_path, now))
    assert len(repo.all('research_events')) == 1
    assert repo.get_control(REPLAY_PRUNED) is None


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


@pytest.mark.parametrize('intraday', [False, True])
def test_apply_requires_backup_and_exclusive_engine_lease(mysql_repositories, tmp_path, intraday):
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment="PAPER_LOCAL")
    seed(repo, "news_events", "expired", now)
    policy = replace(POLICY, intraday_auxiliary=intraday)
    with pytest.raises(ValueError, match="백업"):
        prune_expired(repo, policy, now=now, apply=True)
    owner = mysql_repositories(repo.config.namespace, environment="PAPER_LOCAL")
    with owner.engine_lease(), pytest.raises(RuntimeError, match="다른 엔진"):
        prune_expired(repo, policy, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert len(repo.all("news_events")) == 1 and repo.get_control(REPORT) is None
    live = mysql_repositories(environment="LIVE_LOCAL")
    with pytest.raises(ValueError, match="PAPER_LOCAL"):
        prune_expired(live, policy, now=now, apply=True, receipt=receipt(tmp_path, now))


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


def test_replay_growth_uses_covering_keys_then_bounded_primary_reads(mysql_repositories, tmp_path, monkeypatch):
    now = datetime(2026, 11, 23, 18, tzinfo=timezone.utc)
    repo = mysql_repositories(environment='PAPER_LOCAL')
    other = mysql_repositories(environment='PAPER_LOCAL')
    policy = replace(POLICY, intraday_auxiliary=True)
    prune_expired(repo, policy, now=now, apply=True, receipt=receipt(tmp_path, now))
    for i in range(12):
        seed(other, 'research_events', str(i), now, days=0, kind='engine_batch', run_id='other', data={})
        seed(repo, 'research_events', str(i), now, days=0,
             kind='engine_batch' if i % 2 == 0 else 'unknown_kind', run_id='current', data={})
    original, queries = repo._execute, []
    def record(sql, params=(), **kwargs):
        if kwargs.get('optional_observation') and 'pq_research_events' in sql:
            queries.append((sql, params))
        return original(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', record)
    later = now + timedelta(seconds=60)
    result = prune_expired(repo, policy, now=later, apply=True, receipt=receipt(tmp_path, later))
    generation = result['tables']['research_events']['observation']['generation']
    assert generation['rows_min'] == 6 and generation['exact'] is True
    key_sql, key_params = next((s, p) for s, p in queries if 'seq>%s' in s)
    plan = original('EXPLAIN ' + key_sql, key_params, fetch='all')
    assert 'Using index' in plan[0][-1]
    payloads = [(s, p) for s, p in queries if 'FORCE INDEX (PRIMARY)' in s]
    assert payloads and all(len(p) - 1 <= 10 for _, p in payloads)
    assert result['inspected'] <= policy.max_rows


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


@pytest.mark.parametrize('phase', ['keys', 'payload'])
def test_read_timeout_is_partial_and_never_deletes_an_unfinished_page(mysql_repositories, tmp_path, monkeypatch, phase):
    import pymysql
    from sessionquant.database.mysql_repository import MySQLRepositoryError
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment='PAPER_LOCAL')
    for index in range(25):
        seed(repo, 'research_events', str(index), now, kind='engine_batch', run_id='past', data={})
    execute = repo._execute
    queries = []
    def timeout(sql, params=(), **kwargs):
        if sql.startswith('SELECT /*+ MAX_EXECUTION_TIME(') and 'FROM pq_research_events' in sql:
            queries.append(sql)
            if (phase == 'keys' and 'JSON_OBJECT' not in sql) or (phase == 'payload' and len(queries) >= 3):
                raise MySQLRepositoryError(pymysql.err.OperationalError(3024, 'private SQL'), 'SELECT')
        return execute(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', timeout)
    policy = replace(POLICY, batch_size=25, intraday_auxiliary=True)
    report = prune_expired(repo, policy, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert report['status'] == 'PARTIAL' and report['deleted'] == 0
    assert report['observability']['stop_reason'] == 'QUERY_TIMEOUT'
    assert report['errors'] == [{'table': 'pq_research_events', 'code': 3024, 'phase': phase.upper(), 'message': '자료 조회 시간 초과'}]
    assert not report['tables']['research_events']['complete']
    assert len(repo.all('research_events')) == 25
    assert not repo.get_control(CURSORS, {}).get('positions', {}).get('research_events')
    assert 'private' not in str(report)
    monkeypatch.setattr(repo, '_execute', execute)
    resumed = prune_expired(repo, policy, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert resumed['status'] == 'COMPLETE' and resumed['deleted'] == 25


def test_large_replay_reads_covering_keys_then_bounded_primary_key_payloads(mysql_repositories, tmp_path, monkeypatch):
    import json
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment='PAPER_LOCAL')
    for index in range(23):
        seed(repo, 'research_events', str(index), now, kind='engine_batch', run_id='past',
             data={'irrelevant_large_frame': 'x' * 262144})
    execute = repo._execute
    plans, payload_sizes = [], []
    def capture(sql, params=(), **kwargs):
        if sql.startswith('SELECT /*+ MAX_EXECUTION_TIME(3000)') and 'FROM pq_research_events' in sql:
            if 'JSON_OBJECT' not in sql:
                plan = json.loads(execute('EXPLAIN FORMAT=JSON ' + sql, params, fetch='one')[0])
                plans.append(plan)
                assert 'ix_namespace_captured' in json.dumps(plan)
                assert '"using_filesort": true' not in json.dumps(plan)
            else:
                assert 'FORCE INDEX (PRIMARY)' in sql and 'seq IN (' in sql
                assert 'ORDER BY' not in sql
                payload_sizes.append(len(params) - 1)
        return execute(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', capture)
    policy = replace(POLICY, batch_size=23, intraday_auxiliary=True)
    report = prune_expired(repo, policy, now=now, apply=True, receipt=receipt(tmp_path, now))
    assert report['deleted'] == 23 and report['status'] == 'COMPLETE'
    assert plans and payload_sizes == [10, 10, 3]


@pytest.mark.parametrize('phase', ['keys', 'payload'])
def test_timeout_splits_small_reads_and_preserves_protected_records(mysql_repositories, tmp_path, monkeypatch, phase):
    import pymysql
    from sessionquant.database.mysql_repository import MySQLRepositoryError
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment='PAPER_LOCAL')
    for index in range(25):
        seed(repo, 'research_events', str(index), now, kind='engine_batch', run_id='past', data={})
    seed(repo, 'research_events', 'keep', now, kind='engine_batch', run_id='active', data={})
    repo.set_control('research_recording_v1', {'active': True, 'run_id': 'active'})
    execute = repo._execute
    attempts = []
    def slow_large_batch(sql, params=(), **kwargs):
        if sql.startswith('SELECT /*+ MAX_EXECUTION_TIME(3000)') and 'FROM pq_research_events' in sql:
            if (phase == 'keys') == ('JSON_OBJECT' not in sql):
                attempts.append(params[-1] if phase == 'keys' else tuple(params[1:]))
            if (phase == 'keys' and 'JSON_OBJECT' not in sql and params[-1] > 2
                    or phase == 'payload' and 'JSON_OBJECT' in sql and len(params)-1 > 2):
                raise MySQLRepositoryError(pymysql.err.OperationalError(3024, 'private SQL'), 'SELECT')
        return execute(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', slow_large_batch)
    report = prune_expired(repo, replace(POLICY, batch_size=25, intraday_auxiliary=True), now=now,
                           apply=True, receipt=receipt(tmp_path, now))
    assert report['status'] == 'COMPLETE' and not report['errors'] and report['deleted'] == 25
    assert report['tables']['research_events']['read_splits'] > 0
    assert [row['id'] for row in repo.all('research_events')] == ['keep']
    assert not repo.get_control(CURSORS)['positions'].get('research_events')
    if phase == 'keys':
        # Once a smaller lookup works, later pages never repeat oversized reads.
        assert [size for size in attempts if size > 2] == [25, 12, 6, 3]
    else:
        first_page_end = max(attempts[0])
        later_pages = [ids for ids in attempts if min(ids) > first_page_end]
        assert later_pages and all(len(ids) <= 2 for ids in later_pages)


def test_single_legacy_json_retry_still_checks_protected_evidence(mysql_repositories, tmp_path, monkeypatch):
    import pymysql
    from sessionquant.database.mysql_repository import MySQLRepositoryError
    now = datetime.now(timezone.utc)
    repo = mysql_repositories(environment='PAPER_LOCAL')
    cases = [('expired', 'closed', {}), ('active', 'active', {}),
             ('finance', 'closed', {'news_id': 'used-news'}),
             ('unknown-date', 'closed', {'received_at': 'not-a-date'})]
    for key, run, metadata in cases:
        seed(repo, 'research_events', key, now, kind='engine_batch', run_id=run,
             data={'bulk': 'x' * 1700000, **metadata})
    repo.set_control('research_recording_v1', {'active': True, 'run_id': 'active'})
    seed(repo, 'signals', 'signal', now, news_id='used-news')
    execute = repo._execute
    recovered = []
    def cold_payload(sql, params=(), **kwargs):
        if 'FROM pq_research_events FORCE INDEX (PRIMARY)' in sql and 'JSON_OBJECT' in sql:
            if 'MAX_EXECUTION_TIME(3000)' in sql:
                raise MySQLRepositoryError(pymysql.err.OperationalError(3024, 'fixture'), 'SELECT')
            if 'MAX_EXECUTION_TIME(10000)' in sql:
                assert len(params) == 2
                recovered.append(params[1])
        return execute(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', cold_payload)
    report = prune_expired(repo, replace(POLICY, intraday_auxiliary=True), now=now,
                           apply=True, receipt=receipt(tmp_path, now))
    assert report['status'] == 'COMPLETE' and report['deleted'] == 1 and not report['errors']
    assert {r['id'] for r in repo.all('research_events')} == {'active', 'finance', 'unknown-date'}
    assert len(repo.all('signals')) == 1
    assert len(set(recovered)) == 4
    assert report['tables']['research_events']['single_read_retries'] == 4
    assert report['tables']['research_events']['last_single_read_retry']['recovered'] is True


@pytest.mark.parametrize('changed', [None, 'report', 'heartbeat', 'lease'])
def test_expired_cleanup_start_recovery_checks_real_db_without_writes(mysql_repositories, monkeypatch, changed):
    from types import SimpleNamespace
    from test_retention_window_recovery import module, evidence, RECEIPT, UPLOADED
    from sessionquant.database.config import MySQLConfig
    import sessionquant.build_info as build
    import sessionquant.session_config as configs
    host = module('resume_start_after_retention_expiry')
    repo = mysql_repositories(environment='PAPER_LOCAL')
    report, _ = evidence()
    report['namespace'] = repo.namespace
    repo.set_control('data_retention_v1', report)
    repo.set_control('trade_control_state', {'mode': 'paper', 'status': 'OFFLINE', 'running': False, 'armed': False})
    repo._execute('UPDATE pq_controls SET updated_at=%s WHERE namespace=%s AND name=%s',
                  ((UPLOADED + timedelta(seconds=1 if changed == 'heartbeat' else -1)).replace(tzinfo=None),
                   repo.namespace, 'trade_control_state'))
    before = repo._execute('SELECT name,payload,updated_at FROM pq_controls WHERE namespace=%s ORDER BY name',
                           (repo.namespace,), fetch='all')
    monkeypatch.setattr(build, 'read_build_info', lambda: {'revision': host.REVISION})
    monkeypatch.setattr(configs, 'load_session_config', lambda _: SimpleNamespace(
        database_backend='mysql', environment='PAPER_LOCAL', db_path='unused'))
    monkeypatch.setattr(MySQLConfig, 'from_env', classmethod(lambda cls: repo.config))
    expected = host.digest({**report, 'deleted': -1}) if changed == 'report' else host.digest(report)
    if changed == 'lease':
        with repo.engine_lease(), pytest.raises(RuntimeError):
            host.verify_db(RECEIPT, expected)
    elif changed:
        with pytest.raises(ValueError): host.verify_db(RECEIPT, expected)
    else:
        host.verify_db(RECEIPT, expected)
    assert repo._execute('SELECT name,payload,updated_at FROM pq_controls WHERE namespace=%s ORDER BY name',
                         (repo.namespace,), fetch='all') == before
