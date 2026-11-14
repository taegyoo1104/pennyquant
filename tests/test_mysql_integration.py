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

    def create(namespace=None, *, environment=config.environment):
        selected = namespace or "integration_" + uuid4().hex
        repo = MySQLRepository("integration.db", replace(config, namespace=selected, environment=environment))
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


def test_main_cost_panel_reads_paper_namespace_while_account_is_live(mysql_repositories, monkeypatch):
    """실제 계좌 화면에서도 Paper 비용 원장을 선택하고 다른 장부와 합치지 않는다."""
    from datetime import datetime, timezone
    from decimal import Decimal
    from sessionquant.database.config import namespace_for
    from sessionquant.dashboard.summary_panels import open_cost_repository, cost_summary
    path = 'cost-' + uuid4().hex + '.db'
    paper = mysql_repositories(namespace_for(path, environment='PAPER_LOCAL'), environment=None)
    live = mysql_repositories(namespace_for(path, environment='LIVE_LOCAL'), environment=None)
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    paper.set_control('session_llm_budget_v1', {month: {'spent': '0.123456'}})
    paper.set_control('openai_funding_note', {'total_usd': '10'})
    live.set_control('session_llm_budget_v1', {month: {'spent': '99'}})
    monkeypatch.setenv('SQ_DB_BACKEND', 'mysql')
    monkeypatch.setenv('SQ_ENVIRONMENT', 'LIVE_LOCAL')
    reader = open_cost_repository(path, environment='PAPER_LOCAL')
    try:
        assert reader.namespace == paper.namespace
        summary = cost_summary(reader)
        assert summary['spent'] == Decimal('0.123456') and summary['deposited'] == 10
    finally:
        reader.close()


def test_connection_probe_detects_other_mysql_runtime_namespace(mysql_repositories):
    """다른 네임스페이스의 실행 중 수집기를 발견하면 토큰을 재발급하지 않는다."""
    from sessionquant.connection_checks import active_collector
    paper = mysql_repositories()
    live = mysql_repositories()
    live.set_control('account_monitor_status', {'status': 'ok', 'updated_at': datetime.now(timezone.utc).isoformat()})
    assert active_collector(paper)


async def test_real_mysql_watchlist_snapshot_focus_and_fair_order_survive_reconnection(mysql_repositories, tmp_path):
    """격리된 MySQL 원장에서 합성 100→20 근거와 조회 순서를 다시 읽는다."""
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_config import SessionConfig
    from sessionquant.watchlist.selection import WatchlistManager
    from watchlist_fixtures import NOW, Provider, cap_batch, observe

    repo = mysql_repositories()
    universe = UniverseStore(tmp_path / 'members.json')
    universe.refresh([f'T{i:03}' for i in range(500)], source='SYNTHETIC_FIXTURE',
                     observed_at=NOW-timedelta(days=1))
    config = SessionConfig(database_backend='mysql', market_cap_input=tmp_path / 'unused.json')
    manager = WatchlistManager(repo, universe, config, provider=Provider(cap_batch()), allow_synthetic=True)
    await manager.refresh(NOW)
    now = observe(manager)
    assert manager.focus(now) == [f'T{i:03}' for i in range(400, 420)]
    manager.scheduler.mark('T400', now)
    manager.scheduler.mark('T401', now)
    manager.publish(now)

    reader = mysql_repositories(repo.namespace)
    restored = WatchlistManager(reader, universe, config, allow_synthetic=True)
    assert restored.store.at(now)['id'] == manager.store.at(now)['id']
    assert len(restored.store.status(now, allow_synthetic=True)['items']) == 100
    assert restored.scheduler.choose(restored.focus(now), 2) == ['T402', 'T403']
    view = reader.get_control('watchlist_dashboard_v1')
    assert view['snapshot']['synthetic'] is True and view['focus_count'] == 20
    assert any(row.get('kind') == 'watchlist_focus' and row['items'] for row in reader.all('pipeline_status'))
    assert reader.all('orders') == []


def test_real_mysql_market_quality_metadata_and_strict_recovery(mysql_repositories):
    """합성 시세의 출처·미확인 자격·품질 보고서를 격리 MySQL에서 복구한다."""
    from sessionquant.market.eligibility import toss_eligibility
    from sessionquant.market.quality import QUALITY_KEY
    from sessionquant.market.universe import UniverseStore
    from sessionquant.models import Quote, jsonable
    from sessionquant.session_engine import SessionEngine
    from test_market_data_quality import NOW, THROUGH, SOURCE, minutes, stock
    repo = mysql_repositories()
    universe = UniverseStore()
    universe.refresh(['TEST'],source='SYNTHETIC_FIXTURE',observed_at=NOW-timedelta(days=1))
    engine = SessionEngine(repo,universe,synthetic=True,strict_market_data=True)
    records = [{'kind':'bar','data':jsonable(bar)} for bar in minutes()]
    records += [{'kind':'eligibility','data':jsonable(toss_eligibility('TEST',stock(),NOW))},
                {'kind':'quote','data':jsonable(Quote('TEST',THROUGH,NOW,100,100.01,100,100,False,False,'NASDAQ'))}]
    engine.apply_batch(records,NOW)
    report = repo.get_control(QUALITY_KEY)
    assert report['required_data_coverage']['rvol']['verified'] == 0
    assert report['blocker_counts']['eligibility_unverified'] == 1
    reader = mysql_repositories(repo.namespace)
    restored = SessionEngine(reader,universe,synthetic=True,strict_market_data=True,restore=True)
    assert restored.market.bars['TEST'][0].source == SOURCE
    assert restored.market.eligibility['TEST'].account_scope == 'UNKNOWN'
    assert reader.get_control(QUALITY_KEY) == report and reader.all('orders') == []
    with pytest.raises(ValueError):
        SessionEngine(reader,universe,synthetic=True,strict_market_data=False,restore=True)


def test_real_mysql_paper_market_partial_cancel_race_preserves_audit_and_money(mysql_repositories):
    """합성 체결의 미확정 취소·금액·비교 근거를 격리 MySQL에서 복구한다."""
    from decimal import Decimal
    from sessionquant.market.universe import UniverseStore
    from sessionquant.portfolio import PortfolioSettings
    from sessionquant.session_engine import SessionEngine
    from test_paper_market_fills import NOW, intent, quote
    settings = PortfolioSettings(paper_cancel_latency_seconds=Decimal('2'))
    repo = mysql_repositories()
    universe = UniverseStore()
    engine = SessionEngine(repo,universe,settings=settings,synthetic=True)
    engine.broker.submit_order(intent())
    engine.broker.process_quote(quote(1),max_fill_qty=Decimal('.05'))
    engine.broker.cancel_order('buy',NOW+timedelta(seconds=2))
    engine.tick(NOW+timedelta(seconds=2))
    reader = mysql_repositories(repo.namespace)
    restored = SessionEngine(reader,universe,settings=settings,restore=True,synthetic=True)
    assert restored.broker.orders['buy'].status=='cancel_pending'
    restored.broker.process_quote(quote(3),max_fill_qty=Decimal('.05'))
    restored.tick(NOW+timedelta(seconds=4))
    assert restored.broker.positions['AAPL'].qty==Decimal('.1')
    order = reader.all('orders')[0]
    assert order['status']=='cancelled' and order['fill_model_version']=='market_v2'
    assert Decimal(order['simulated_fill_price'])==Decimal('100.1')
    assert len(reader.all('executions'))==2 and restored.broker.reserved_cash==0
    assert restored.broker.reconcile()['ok']
