"""Opt-in tests against an actual MySQL 8 server, with isolated namespaces.

Set PQ_TEST_MYSQL=1 and PQ_MYSQL_* connection variables. Every test uses a
random namespace and removes only its own rows. No tables or schemas are dropped.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.getenv("PQ_TEST_MYSQL") != "1", reason="requires opt-in real MySQL 8")


@pytest.fixture
def mysql_repositories():
    from sessionquant.database.config import MySQLConfig
    from sessionquant.database.mysql_repository import MySQLRepository
    from sessionquant.database.repository import KINDS
    import pymysql

    config = MySQLConfig.from_env()
    repositories = []

    def create(namespace=None):
        selected = namespace or "integration_" + uuid4().hex
        repo = MySQLRepository("integration.db", replace(config, namespace=selected))
        repositories.append(repo)
        return repo

    yield create

    for repo in repositories:
        with suppress(Exception):
            repo.db.rollback()
            with repo.db.cursor() as cursor:
                for name in [*("pq_" + kind for kind in KINDS), "pq_engine_state", "pq_controls", "sq_financial_ledger"]:
                    cursor.execute(f"DELETE FROM `{name}` WHERE namespace=%s", (repo.namespace,))
            repo.db.commit()
        with suppress(pymysql.Error):
            repo.close()


def test_real_mysql_unicode_upserts_and_anonymous_journal_rows(mysql_repositories):
    repo = mysql_repositories()
    repo.record("orders", {"id": "주문-" + "가" * 400, "symbol": "TEST", "qty": 1, "reason": "첫 접수"})
    repo.record("orders", {"id": "second", "symbol": "OTHER", "qty": 2})
    repo.record("orders", {"id": "주문-" + "가" * 400, "symbol": "TEST", "qty": 1, "reason": "체결 완료"})
    records = repo.all("orders")
    assert len(records) == 2 and records[0]["reason"] == "체결 완료"
    assert [row["id"] for row in repo.recent("orders", 1)] == ["second"]
    repo.log("INFO", "첫 번째 익명 로그")
    repo.log("INFO", "두 번째 익명 로그")
    assert len(repo.all("system_logs")) == 2


def test_real_mysql_nested_transaction_rolls_back_state_and_events(mysql_repositories):
    repo = mysql_repositories()
    repo.save_state({"account": {"cash": 70}})
    repo.set_control("marker", "before")
    with pytest.raises(RuntimeError, match="rollback test"):
        with repo.transaction():
            repo.record("executions", {"id": "not-committed", "symbol": "TEST", "qty": 1})
            with repo.transaction():
                repo.save_state({"account": {"cash": 40}})
                repo.set_control("marker", "after")
            raise RuntimeError("rollback test")
    assert repo.all("executions") == []
    assert repo.load_state() == {"account": {"cash": 70}}
    assert repo.get_control("marker") == "before"


def test_real_mysql_namespace_isolates_records_controls_and_positions(mysql_repositories):
    first, second = mysql_repositories(), mysql_repositories()
    first.record("orders", {"id": "same-id", "symbol": "FIRST"})
    second.record("orders", {"id": "same-id", "symbol": "SECOND"})
    first.set_control("mode", "paper")
    second.set_control("mode", "live")
    first.replace_positions({"FIRST": {"symbol": "FIRST", "qty": 2}})
    second.replace_positions({"SECOND": {"symbol": "SECOND", "qty": 3}})
    first.replace_positions({})
    assert first.all("orders")[0]["symbol"] == "FIRST"
    assert second.all("orders")[0]["symbol"] == "SECOND"
    assert first.get_control("mode") == "paper" and second.get_control("mode") == "live"
    assert first.all("positions") == [] and second.all("positions")[0]["qty"] == 3


def test_real_mysql_engine_advisory_lock_blocks_another_connection(mysql_repositories):
    namespace = "lease_" + uuid4().hex
    first, second = mysql_repositories(namespace), mysql_repositories(namespace)
    with first.engine_lease():
        with pytest.raises(RuntimeError):
            with second.engine_lease():
                pytest.fail("A second writer acquired the same namespace")
    with second.engine_lease():
        second.set_control("after_lease", True)
    assert first.get_control("after_lease") is True


def test_real_mysql_storage_stats_and_pruning_preserve_financial_journal(mysql_repositories):
    repo = mysql_repositories()
    old = datetime.now(timezone.utc) - timedelta(days=10)
    repo.record("price_samples", {"id": "old-price", "symbol": "TEST", "last_price": 2})
    repo.record("price_samples", {"id": "current-price", "symbol": "TEST", "last_price": 3})
    repo.record("orders", {"id": "old-order", "symbol": "TEST", "qty": 1})
    repo.save_state({"positions": {"TEST": {"qty": 1}}})
    with repo.db.cursor() as cursor:
        for table, identity in (("pq_price_samples", "old-price"), ("pq_orders", "old-order")):
            cursor.execute(f"UPDATE `{table}` SET captured_at=%s WHERE namespace=%s "
                           "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.id'))=%s",
                           (old.replace(tzinfo=None), repo.namespace, identity))
    repo.db.commit()
    stats = repo.storage_stats()
    assert stats["backend"] == "mysql" and stats["namespace"] == repo.namespace
    assert stats["namespace_rows"]["price_samples"] == 2
    assert stats["namespace_rows"]["orders"] == stats["namespace_rows"]["engine_state"] == 1
    assert stats["namespace_json_bytes"]["orders"] > 0 and stats["namespace_payload_bytes"] > 0
    assert stats["physical_table_bytes"] > 0 and "all namespaces" in stats["physical_scope"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    preview = repo.prune(cutoff, kinds=("price_samples",), batch_size=1, dry_run=True)
    assert preview["price_samples"] == 1 and len(repo.all("price_samples")) == 2
    removed = repo.prune(cutoff, kinds=("price_samples",), batch_size=1, dry_run=False)
    assert removed["price_samples"] == 1
    assert [row["id"] for row in repo.all("price_samples")] == ["current-price"]
    assert len(repo.all("orders")) == 1 and repo.load_state()["positions"]["TEST"]["qty"] == 1
    with pytest.raises(ValueError):
        repo.prune(cutoff, kinds=("orders",), dry_run=False)


def test_real_mysql_engine_persist_and_restore_book_actual_simulated_fills(mysql_repositories):
    from sessionquant.config.settings import Settings
    from sessionquant.engine import EventEngine
    from sessionquant.events import Event
    from sessionquant.models import Quote, Signal

    repo = mysql_repositories()
    config = Settings(initial_cash=1000, regular_hours_only=False, fee_model="toss_us", exit_price_policy="net")
    engine = EventEngine(config, repository=repo)
    at = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
    quote = Quote("TEST", at, at, 2.0, 2.004, 100, 100)
    signal = Signal("test-signal", "TEST", "momentum", at, 2.004, "Integration fixture")
    assert engine.manager.submit(signal, quote, at) is not None
    later = at + timedelta(seconds=1)
    engine.process(Event("quote", Quote("TEST", later, later, 2.0, 2.004, 100, 100)))
    assert engine.manager.account.positions["TEST"].qty > 0
    assert len(repo.all("executions")) == 1
    restored = EventEngine(config, repository=repo, restore=True)
    assert restored.manager.account.positions["TEST"].qty == engine.manager.account.positions["TEST"].qty
    assert restored.manager.account.cash == engine.manager.account.cash
    assert len(restored.manager.fills) == 1
