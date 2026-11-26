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


def test_mysql_pipeline_materialized_summary_preserves_large_payloads_and_old_writers(mysql_repositories, monkeypatch):
    import json
    from sessionquant.database import pipeline_summary as cached
    from sessionquant.database.mysql_repository import MySQLRepository
    from sessionquant.database.pipeline_projection import expression, summary
    from test_pipeline_projection import assert_projection

    repo = mysql_repositories()
    assert cached.available(repo), 'integration fixture must enable summary triggers'
    repo._execute('INSERT INTO pq_pipeline_status(namespace,payload,captured_at) VALUES(%s,%s,CURRENT_TIMESTAMP(6))',
                  (repo.namespace, json.dumps({'id': 'raw-insert', 'approved': False})))
    assert repo.recent_pipeline_summary(1) == [{'id': 'raw-insert', 'approved': False}]
    repo._execute('DELETE FROM pq_pipeline_status WHERE namespace=%s', (repo.namespace,))
    assert_projection(repo)
    repo.record('pipeline_status', {'id': 'large', 'kind': 'legacy', 'reason': '한' * 1000,
        'approved': False, 'included_count': 0, 'status': None, 'items': [{'body': 'x' * 2200000}]})
    expected = summary(repo._execute(f"SELECT {expression('mysql')} FROM pq_pipeline_status "
        'WHERE namespace=%s ORDER BY seq DESC LIMIT 1', (repo.namespace,), fetch='one')[0])
    seq, stamp = repo._execute('SELECT seq,updated_at FROM pq_pipeline_status WHERE namespace=%s '
        'ORDER BY seq DESC LIMIT 1', (repo.namespace,), fetch='one')
    other = mysql_repositories()
    other.record('pipeline_status', {'id': 'private', 'status': 'other namespace'})
    reader = MySQLRepository('integration.db', repo.config, initialize=False)
    try:
        calls = []
        execute = reader._execute
        def tracked(sql, *args, **kwargs):
            calls.append(sql)
            return execute(sql, *args, **kwargs)
        monkeypatch.setattr(reader, '_execute', tracked)
        assert reader.recent_pipeline_summary(1) == [expected]
        assert not any('JSON_EXTRACT(' in sql or 'SELECT payload' in sql for sql in calls)
        # An old writer using raw SQL, even with unchanged updated_at, must
        # update the small summary in the same transaction.
        repo._execute("UPDATE pq_pipeline_status SET payload=JSON_SET(payload,'$.status','changed'),updated_at=%s "
                      'WHERE seq=%s', (stamp, seq))
        assert reader.recent_pipeline_summary(1)[0]['status'] == 'changed'
        with pytest.raises(RuntimeError):
            with repo.transaction():
                repo._execute("UPDATE pq_pipeline_status SET payload=JSON_SET(payload,'$.status','rollback') WHERE seq=%s", (seq,))
                assert repo.recent_pipeline_summary(1)[0]['status'] == 'rollback'
                raise RuntimeError('rollback')
        assert reader.recent_pipeline_summary(1)[0]['status'] == 'changed'
        repo.record('pipeline_status', {'id': 'large', 'kind': 'legacy', 'approved': True})
        assert reader.recent_pipeline_summary(1) == [{'id': 'large', 'kind': 'legacy', 'approved': True}]
        raw = repo._execute(f'SELECT summary FROM {cached.TABLE} WHERE source_seq=%s', (seq,), fetch='one')[0]
        assert len(json.dumps(json.loads(raw))) < 5000
        repo._execute('DELETE FROM pq_pipeline_status WHERE namespace=%s AND seq=%s', (repo.namespace, seq))
        assert repo._execute(f'SELECT 1 FROM {cached.TABLE} WHERE source_seq=%s', (seq,), fetch='one') is None
        assert other.recent_pipeline_summary(1)[0]['id'] == 'private'
    finally:
        reader.close()


def test_mysql_pipeline_missing_stale_and_wrong_version_summaries_use_recent_window(mysql_repositories, monkeypatch):
    from sessionquant.database import pipeline_summary as cached
    repo = mysql_repositories()
    for index in range(6):
        repo.record('pipeline_status', {'id': str(index), 'approved': bool(index % 2), 'rows': ['x' * 10000]})
    keys = repo._execute('SELECT seq FROM pq_pipeline_status WHERE namespace=%s ORDER BY seq DESC LIMIT 3',
                         (repo.namespace,), fetch='all')
    repo._execute(f'DELETE FROM {cached.TABLE} WHERE source_seq=%s', (keys[0][0],))
    repo._execute(f'UPDATE {cached.TABLE} SET source_updated_at=source_updated_at-INTERVAL 1 SECOND WHERE source_seq=%s', (keys[1][0],))
    repo._execute(f'UPDATE {cached.TABLE} SET version=999 WHERE source_seq=%s', (keys[2][0],))
    execute = repo._execute
    fallback_ids = []
    def tracked(sql, params=(), **kwargs):
        if sql.startswith('SELECT seq,JSON_ARRAY('):
            fallback_ids.extend(params[1:])
        return execute(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', tracked)
    assert [r['id'] for r in repo.recent_pipeline_summary(3)] == ['3', '4', '5']
    assert fallback_ids == [row[0] for row in keys]


def test_mysql_credit_projection_matches_sqlite_legacy_time_semantics(mysql_repositories,tmp_path):
    from decimal import Decimal
    from sessionquant.database.repository import Repository
    from sessionquant.llm.costs import credit_summary,set_manual_credit
    now=datetime(2026,11,20,16,tzinfo=timezone.utc);checked=now-timedelta(hours=1)
    repo=mysql_repositories();other=mysql_repositories()
    sqlite=Repository(tmp_path/'credit-comparison.db',backend='sqlite')
    rows=[
        {'timestamp':'2024-02-29T10:00:00+00:00'},
        {'timestamp':'2026-11-19T10:00:00.123456+00:00','request_started_at':'2026-11-19T09:00:00+00:00'},
        {'timestamp':'2026-02-30T10:00:00+00:00'},
        {'timestamp':'2026-11-19T25:00:00+00:00'},
        {'timestamp':'0000-01-01T10:00:00+00:00'},
        {'timestamp':{'bad':'time'}},
        {'timestamp':'2026-11-19T10:00:00'},
        {'timestamp':None},
        {'timestamp':now.isoformat(),'request_started_at':(checked-timedelta(seconds=1)).isoformat()},
        {'timestamp':now.isoformat(),'request_started_at':[]},
        {'timestamp':now.isoformat(),'request_started_at':False},
        {'timestamp':now.isoformat(),'request_started_at':'2026-11-20T16:00:00+01:00'},
        {'timestamp':now.isoformat(),'cost_usd':False},
    ]
    try:
        for index,row in enumerate(rows):
            value={'id':str(index),'cost_usd':'0.123456789012345678901','unused_body':'x'*20000,**row}
            repo.record('llm_usage',value);sqlite.record('llm_usage',value)
        for target in (repo,sqlite):set_manual_credit(target,'10',checked,now=now)
        mysql_rows=list(repo.iter_credit_usage(checked,batch_size=3))
        assert mysql_rows==list(sqlite.iter_credit_usage(checked,batch_size=3))
        assert len(mysql_rows)==len(rows)-2 and len(str(mysql_rows))<3000
        assert not list(other.iter_credit_usage(checked))
        summary=credit_summary(repo,now)
        assert summary==credit_summary(sqlite,now)
        assert summary['estimated_credit_usd'] is None
        assert summary['local_cost_since_check_usd']==Decimal('0.370370367037037036703')
    finally:sqlite.close()


def test_mysql_unchanged_history_skips_sql_but_corrections_and_timing_survive_restart(mysql_repositories):
    from sessionquant.market.history import HistoryStore,history_plan
    from sessionquant.performance import capture
    from test_watchlist_history import NOW,minute_rows
    repo=mysql_repositories()
    store=HistoryStore(repo)
    day,opened,_=history_plan(NOW)[-1]
    rows=minute_rows('AAPL',opened,count=2)
    store.save('AAPL',day,rows)
    with capture() as repeated:
        store.save('AAPL',day,[replace(b,received_at=NOW+timedelta(seconds=10)) for b in rows])
    assert not any(r['phase']=='db_query' for r in repeated.snapshot()['phases'])
    with capture() as changed:
        store.save('AAPL',day,[replace(rows[0],volume=999)])
    assert any(r['phase']=='db_query' and r['calls']>=1 for r in changed.snapshot()['phases'])
    reopened=mysql_repositories(repo.config.namespace,environment=repo.config.environment)
    row,bars=HistoryStore(reopened).get('AAPL',day)
    assert len(bars)==3 and row['quality']['conflicting_minutes']==1
    assert repo.all('orders')==[]


async def test_mysql_startup_repair_and_readiness_survive_new_connection(mysql_repositories):
    from types import SimpleNamespace
    from sessionquant.market.history import HistoryStore
    from sessionquant.market.warmup import StartupWarmup, CONTROL
    from sessionquant.watchlist.history_selection import DailyHistorySelection
    from sessionquant.trading_controls import begin_session, heartbeat, request_command
    from test_history_selection import prepare
    from test_startup_warmup import NOW, Provider, finish
    repo=mysql_repositories();other=mysql_repositories()
    store=HistoryStore(repo)
    prepare(store,'AAPL',NOW,omit_last=True)
    control=begin_session(repo,mode='paper',now=NOW)
    clock=SimpleNamespace(now=NOW)
    selection=DailyHistorySelection(repo,startup_pending=True)
    worker=StartupWarmup(repo,store,selection,control,clock=lambda:clock.now)
    heartbeat(repo,control,now=NOW)
    reopened=mysql_repositories(repo.config.namespace,environment=repo.config.environment)
    assert reopened.namespace==repo.namespace
    with pytest.raises(ValueError):
        request_command(reopened,'start',run_id=control['run_id'],environment='PAPER_LOCAL',now=NOW)
    manager=SimpleNamespace(allow_synthetic=False,base_symbols=lambda _:['AAPL'],
        store=SimpleNamespace(status=lambda *args,**kwargs:{'status':'VALID'}))
    await finish(worker,manager,Provider(clock))
    heartbeat(repo,control,now=NOW)
    assert reopened.get_control(CONTROL)['status']=='READY'
    assert reopened.get_control('trade_control_state')['data_ready'] is True
    assert other.get_control(CONTROL) is None
    assert repo.all('orders')==other.all('orders')==[]


def test_real_mysql_rvol_history_restart_namespace_and_schema7(mysql_repositories):
    from sessionquant.market.history import HistoryStore, KIND
    from test_watchlist_history import seeded, NOW
    repo,other = mysql_repositories(),mysql_repositories()
    repo.record('orders',{'id':'existing-order','qty':'0.123456'})
    seeded(repo)
    reopened = mysql_repositories(repo.namespace)
    report = HistoryStore(reopened).report('AAPL',NOW)
    assert report['calculated_value']==2 and report['value'] is None
    assert report['complete_sessions']==20 and len(reopened.all(KIND))==21
    assert other.all(KIND)==[] and repo.all('orders')[0]['qty']=='0.123456'
    with repo.db.cursor() as cursor:
        cursor.execute('SELECT version FROM sq_schema_versions WHERE version=7')
        assert cursor.fetchone()[0]==7


async def test_mysql_history_inspections_skip_payloads_and_detect_sql_change(mysql_repositories,monkeypatch):
    from sessionquant.market.history import HistoryStore,KIND,history_plan,record_id
    from sessionquant.market.history_inspections import CONTROL
    from test_history_selection import prepare
    from test_history_inspection_cache import worker,SUNDAY
    from test_startup_warmup import finish
    repo=mysql_repositories();other=mysql_repositories()
    prepare(HistoryStore(repo),'AAPL',SUNDAY)
    first,manager,provider=worker(repo)
    await finish(first,manager,provider)
    reopened=mysql_repositories(repo.config.namespace,environment=repo.config.environment)
    with monkeypatch.context() as patch:
        def no_decode(*_):pytest.fail('MySQL cache hit must not download/parse full history')
        patch.setattr('sessionquant.market.history_inspections.decode_history',no_decode)
        second,manager,provider=worker(reopened)
        await finish(second,manager,provider)
        assert second.report['reused_history_tasks']==20 and second.report['required_tasks']==0
        assert not provider.calls
    assert other.get_control(CONTROL) is None
    day,opened,_=history_plan(SUNDAY)[-1]
    key=record_id('AAPL',day)
    before=repo.record_fingerprints(KIND,[key])
    with repo.db.cursor() as cursor:
        cursor.execute("UPDATE pq_market_history_sessions SET payload=JSON_REMOVE(payload,'$.bars[10]') WHERE namespace=%s AND record_id=%s",(repo.namespace,key))
    assert reopened.record_fingerprints(KIND,[key])!=before
    third,manager,provider=worker(reopened)
    await finish(third,manager,provider)
    assert third.report['reused_history_tasks']==19 and third.report['required_tasks']==1
    assert provider.calls==[('AAPL',opened+timedelta(minutes=11))]
    assert third.report['status']=='READY'


def test_real_mysql_prepare_paper_preserves_history_and_does_not_copy_trades(mysql_repositories,tmp_path,monkeypatch):
    from pathlib import Path
    from sessionquant.market.history import KIND
    from sessionquant.paper_operations import prepare_paper
    from sessionquant.session_config import SessionConfig
    from sessionquant.market.paper_policy import OBSERVED
    from test_watchlist_history import seeded
    from sessionquant.database.config import MySQLConfig
    src,dst=mysql_repositories(),mysql_repositories()
    seeded(src)
    src.record('orders',{'id':'old-order','qty':'0.123456'})
    base=MySQLConfig.from_env()
    # _open의 실제 파일 잠금·MySQL lease·복사를 유지하고 두 테스트 namespace에 매핑한다.
    from sessionquant import paper_operations
    monkeypatch.setenv('SQ_ENVIRONMENT','PAPER_LOCAL')
    monkeypatch.setenv('SQ_DB_BACKEND','mysql')
    def open_fixture(path,backend):
        namespace=src.namespace if Path(path).name=='source.db' else dst.namespace
        from sessionquant.database.mysql_repository import MySQLRepository
        from contextlib import ExitStack
        from sessionquant.locking import exclusive_lock
        stack=ExitStack()
        stack.enter_context(exclusive_lock(str(path)+'.engine.lock',message='test engine active'))
        repo=MySQLRepository(path,replace(base,namespace=namespace,environment=None))
        stack.callback(repo.close);stack.enter_context(repo.engine_lease())
        return stack,repo
    monkeypatch.setattr(paper_operations,'_open',open_fixture)
    monkeypatch.delenv('SQ_DB_NAMESPACE',raising=False)
    monkeypatch.delenv('PQ_DB_NAMESPACE',raising=False)
    source=SessionConfig(db_path=tmp_path/'source.db')
    target=replace(source,db_path=tmp_path/'target.db',paper_data_policy=OBSERVED)
    report=prepare_paper(source,target)
    assert report['copied_symbol_days']==21
    assert dst.all(KIND)==src.all(KIND) and dst.all('orders')==[]
    assert src.all('orders')[0]['qty']=='0.123456'
    with pytest.raises(ValueError,match='이미'):prepare_paper(source,target)


async def test_mysql_incomplete_history_retry_survives_reconnect_and_detects_changes(mysql_repositories,monkeypatch):
    from sessionquant.market.history import KIND,record_id
    from sessionquant.market.history_inspections import CONTROL
    from test_history_inspection_cache import missing_history,worker,SUNDAY
    from test_startup_warmup import finish
    repo=mysql_repositories();other=mysql_repositories()
    day,_,closed=await missing_history(repo)
    reopened=mysql_repositories(repo.config.namespace,environment=repo.config.environment)
    with monkeypatch.context() as patch:
        def no_decode(*_):pytest.fail('recent incomplete MySQL history must not be downloaded/parsed again')
        patch.setattr('sessionquant.market.history_inspections.decode_history',no_decode)
        second,manager,provider=worker(reopened,now=SUNDAY+timedelta(minutes=1))
        await finish(second,manager,provider)
        assert second.report['deferred_history_tasks']==1 and not provider.calls
        assert second.report['status']=='PARTIAL' and second.report['history_ready_count']==0
    assert other.get_control(CONTROL) is None
    key=record_id('AAPL',day)
    before=reopened.record_fingerprints(KIND,[key])
    with repo.db.cursor() as cursor:
        cursor.execute("UPDATE pq_market_history_sessions SET payload=JSON_REMOVE(payload,'$.bars[10]') WHERE namespace=%s AND record_id=%s",(repo.namespace,key))
    assert reopened.record_fingerprints(KIND,[key])!=before
    third,manager,provider=worker(reopened,now=SUNDAY+timedelta(minutes=2))
    await finish(third,manager,provider)
    assert third.report['deferred_history_tasks']==0
    assert provider.calls and provider.calls[0]==('AAPL',closed)
    assert third.report['status']=='READY'
    assert not repo.all('orders')


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


def test_mysql_market_dashboard_keeps_data_after_connection_loss_and_recovers(mysql_repositories, monkeypatch):
    from sessionquant.dashboard.session_app import load_database
    import sessionquant.database.repository as module
    import sessionquant.database.config as config

    writer = mysql_repositories()
    writer.save_state({'broker': {'cash': '100', 'positions': {}}})
    writer.set_control('sessionquant_context', {'environment': 'PAPER_LOCAL', 'updated_at': '2026-11-18T05:40:00Z'})
    writer.set_control('watchlist_dashboard_v1', {'status': 'VALID', 'operating_count': 25})
    writer.record('quote_snapshots', {'id': 'q', 'symbol': 'SPY', 'bid': '500', 'ask': '501'})
    writer.record('candidates', {'id': 'c', 'symbol': 'AAPL', 'score': 80})
    reader = mysql_repositories(writer.config.namespace)
    original_recent = reader.recent

    def disconnected_recent(kind, limit):
        if kind == 'candidates':
            writer._execute('KILL CONNECTION %s', (reader.db.thread_id(),))
        return original_recent(kind, limit)

    monkeypatch.setattr(reader, 'recent', disconnected_recent)
    monkeypatch.setattr(config, 'database_backend', lambda: 'mysql')
    monkeypatch.setattr(module, 'Repository', lambda path, **kwargs: reader)
    failed = load_database('unused.db', page='시장·종목')
    assert not failed.connected and '2013' in failed.error
    assert reader._closed
    assert failed.watchlist_state['operating_count'] == 25
    assert failed.tables['quote_snapshots'][0]['symbol'] == 'SPY'
    assert 'candidates' in failed.unavailable
    reopened = mysql_repositories(writer.config.namespace)
    monkeypatch.setattr(module, 'Repository', lambda path, **kwargs: reopened)
    refreshed = load_database('unused.db', page='시장·종목')
    assert refreshed.connected and not refreshed.error and reopened._closed
    assert refreshed.tables['candidates'][0]['symbol'] == 'AAPL'
    assert not writer.all('orders')


def test_mysql_existing_schema_dashboard_connection_reads_fresh_state(mysql_repositories):
    from sessionquant.database.mysql_repository import MySQLRepository
    writer = mysql_repositories()
    writer.set_control('trade_control_state', {'status': 'WAITING'})
    reader = MySQLRepository('integration.db', writer.config, initialize=False)
    try:
        with reader.transaction():
            assert reader.get_control('trade_control_state')['status'] == 'WAITING'
        writer.set_control('trade_control_state', {'status': 'STOPPED'})
        with reader.transaction():
            assert reader.get_control('trade_control_state')['status'] == 'STOPPED'
            reader.set_control('dashboard_request', {'action': 'start'})
        assert writer.get_control('dashboard_request') == {'action': 'start'}
        assert not writer.all('orders')
    finally:
        reader.close()


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
    config = SessionConfig(database_backend='mysql', market_cap_input=tmp_path / 'unused.json', watchlist_size=100)
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


def test_real_mysql_reference_journal_restore_and_namespace_isolation(mysql_repositories):
    """ETF·FRED 원장의 Decimal 문자열·시각·기존 주문 ID를 분리 보존한다."""
    from sessionquant.market_reference.store import ReferenceStore, OBSERVATIONS
    from test_market_reference import NOW, candle, series
    repo, other = mysql_repositories(), mysql_repositories()
    repo.record('orders', {'id': 'existing-order', 'symbol': 'AAPL', 'qty': '0.123456'})
    store = ReferenceStore(repo)
    item = series(store, final='99.125')
    repo.record('fred_reference_observations', {'id': 'fred-fixture', 'series': 'DGS10', 'value': '4.125', 'published_at': None})
    reader = mysql_repositories(repo.namespace)
    restored = ReferenceStore(reader)
    assert restored.publish(NOW)['items']['IEF']['id'] == item['id']
    restored.ingest(candle(price='99.125'))
    assert len(reader.all(OBSERVATIONS)) == 31
    assert reader.all('orders')[0]['id'] == 'existing-order'
    assert reader.all('fred_reference_observations')[0]['published_at'] is None
    assert other.all(OBSERVATIONS) == [] and other.all('orders') == []
    with reader.db.cursor() as cursor:
        cursor.execute('SELECT version FROM sq_schema_versions WHERE version=3')
        assert cursor.fetchone()[0] == 3


def test_real_mysql_strategy_risk_latch_and_pending_cancel_survive_restore(mysql_repositories):
    """합성 위험 경고·매수 취소 대기와 금융 수량을 재연결 후 복구한다."""
    from decimal import Decimal
    from sessionquant.market.universe import UniverseStore
    from sessionquant.market_reference.risk import MacroReferenceRiskMonitor
    from sessionquant.market_reference.store import VIEW
    from sessionquant.portfolio import PortfolioSettings
    from sessionquant.session_engine import SessionEngine
    from test_macro_reference_risk import view
    from test_market_reference import NOW
    from test_paper_market_fills import intent, quote
    repo = mysql_repositories()
    settings = PortfolioSettings(paper_cancel_latency_seconds=Decimal('2'))
    options = {'enabled': True, 'paper_shadow_only': False}
    monitor = MacroReferenceRiskMonitor(repo, options, research_only=True)
    repo.set_control(VIEW, view(risk=True, down=True))
    engine = SessionEngine(repo, UniverseStore(), settings=settings, synthetic=True, macro_reference_risk=monitor)
    engine.broker.submit_order(intent(at=NOW))
    engine.broker.process_quote(quote(1, at=NOW), max_fill_qty=Decimal('.05'))
    engine.tick(NOW+timedelta(seconds=1))
    assert engine.broker.get_order('buy').status == 'cancel_pending'
    reader = mysql_repositories(repo.namespace)
    restored_monitor = MacroReferenceRiskMonitor(reader, options, research_only=True)
    restored = SessionEngine(reader, UniverseStore(), settings=settings, synthetic=True, restore=True,
        macro_reference_risk=restored_monitor)
    restored.tick(NOW+timedelta(seconds=3))
    assert restored.broker.get_order('buy').status == 'cancelled'
    assert restored.broker.positions['AAPL'].qty == Decimal('.05')
    assert not [o for o in restored.broker.orders.values() if o.intent.side == 'sell']
    assert restored_monitor.state['latched']
    assert reader.get_control('sessionquant_context')['risk_policy_version'] == 2


def test_real_mysql_research_journal_revisions_rollback_and_namespace(mysql_repositories,monkeypatch,tmp_path):
    """원래 분석 ID는 유지하고 별도 불변 기록·익명화·원자 복구를 검사한다."""
    from sessionquant.research import journal
    from sessionquant.research.bundle import export_recording,load_bundle
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_engine import SessionEngine
    from test_paper_market_fills import NOW
    repo,other=mysql_repositories(),mysql_repositories()
    clock=[NOW]
    monkeypatch.setattr(journal,'clock',lambda:clock[0])
    engine=SessionEngine(repo,UniverseStore(),synthetic=True,record_inputs=True)
    journal.start(repo,engine,source='toss')
    repo.record('news_analysis',{'id':'same','model':'fixture','sentiment':'NEUTRAL','analyzed_at':NOW.isoformat()})
    clock[0]+=timedelta(seconds=1)
    repo.record('news_analysis',{'id':'same','model':'fixture','sentiment':'NEGATIVE','analyzed_at':NOW.isoformat()})
    assert len(repo.all('news_analysis'))==1
    assert len([row for row in repo.all('research_events') if row['kind']=='news_analysis'])==2
    count=len(repo.all('research_events'))
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.record('news_analysis',{'id':'rollback','model':'fixture'})
            raise RuntimeError('원자성 검사')
    assert repo.get_record('news_analysis','rollback') is None and len(repo.all('research_events'))==count
    reader=mysql_repositories(repo.namespace)
    assert list(reader.iter_records('research_events'))==list(repo.iter_records('research_events'))
    assert list(other.iter_records('research_events'))==[]
    journal.stop(repo)
    output=tmp_path/'mysql-bundle';export_recording(reader,output)
    assert load_bundle(output)['record_count']>0
    with reader.db.cursor() as cursor:
        cursor.execute('SELECT version FROM sq_schema_versions WHERE version=4')
        assert cursor.fetchone()[0]==4


def test_real_mysql_stage06_schedule_atomic_claim_and_counters(mysql_repositories):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sessionquant.news.schedule import NewsSchedule
    first = mysql_repositories()
    second = mysql_repositories(first.namespace)
    now = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)
    barrier = Barrier(2)
    def worker(repo):
        schedule = NewsSchedule(repo, clock=lambda: now)
        barrier.wait()
        claimed = schedule.claim('gdelt', 900, now)
        for _ in range(15): schedule.begin('rss', now)
        return claimed
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, [first, second])) == [False, True]
    schedule = NewsSchedule(first, clock=lambda: now)
    assert schedule.state['rss']['calls'] == 30
    assert schedule.state['gdelt']['calls'] == 1
    assert not schedule.due('gdelt', 0, now+timedelta(seconds=299))


def test_real_mysql_stage06_journals_schema_namespace_and_order_preservation(mysql_repositories):
    from sessionquant.news.intake import archive
    from sessionquant.macro.models import EconomicEvent
    from test_stage06_regressions import minimal_pipeline
    repo, other = mysql_repositories(), mysql_repositories()
    now = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)
    repo.record('orders', {'id': 'stage05-order', 'qty': '0.123456'})
    first = archive(repo, {'id': 'article', 'body': 'original', 'received_at': now.isoformat()}, provider='fixture')
    later = archive(repo, {'id': 'article', 'body': 'original', 'received_at': (now+timedelta(seconds=1)).isoformat()}, provider='fixture')
    assert later['duplicate'] and later['first_available_at'] == first['first_available_at']
    repo.record('news_enrichments', {'id': 'enrichment', 'status': 'RIGHTS_UNKNOWN', 'body': None})
    repo.record('macro_context_snapshots', {'id': 'context', 'causal_inference_verified': False})
    pipeline = minimal_pipeline(repo)
    event = EconomicEvent('original', 'CPI', now, now-timedelta(days=1), 'https://www.bls.gov/news.release/cpi.htm',
                          verified=True, logical_id='cpi')
    pipeline.add_economic_event(event)
    pipeline.add_economic_event(replace(event, id='cancel', revision=1, status='CANCELLED', known_at=now+timedelta(minutes=1)))
    reader = mysql_repositories(repo.namespace)
    restored = minimal_pipeline(reader)
    assert restored.risk_context(now)['blackout']
    assert not restored.risk_context(now+timedelta(minutes=1))['blackout']
    assert reader.all('orders')[0] == {'id': 'stage05-order', 'qty': '0.123456'}
    for kind in ('news_ingestions', 'news_enrichments', 'macro_context_snapshots', 'economic_events'):
        assert reader.all(kind) and other.all(kind) == []
    with reader.db.cursor() as cursor:
        cursor.execute('SELECT version FROM sq_schema_versions WHERE version=5')
        assert cursor.fetchone()[0] == 5


def test_real_mysql_stage06_diagnostics_sql_is_select_only_and_scoped(mysql_repositories):
    from pathlib import Path
    from sessionquant.news.schedule import NewsSchedule
    repo = mysql_repositories()
    other = mysql_repositories()
    repo.record('news_ingestions', {'id': 'one', 'provider': 'fixture', 'eligible_for_llm': False})
    other.record('news_ingestions', {'id': 'other', 'provider': 'other', 'eligible_for_llm': True})
    NewsSchedule(repo).begin('rss:fixture')
    sql = (Path(__file__).resolve().parents[1]/'sql/news_enrichment_diagnostics.sql').read_text()
    sql = '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
    statements = [part.strip() for part in sql.replace('REPLACE_WITH_PAPER_NAMESPACE', repo.namespace).split(';') if part.strip()]
    results = []
    with repo.db.cursor() as cursor:
        for statement in statements:
            assert statement.startswith('SELECT')
            cursor.execute(statement)
            results.append(cursor.fetchall())
    assert results[0][0] == ('news_ingestions', 1)
    assert results[1][0][0] == 'fixture'
    assert results[4][0][0] == 'rss:fixture'
    assert len(repo.all('news_ingestions')) == len(other.all('news_ingestions')) == 1


async def test_real_mysql_stage07_call_usage_join_sql_and_namespace(mysql_repositories):
    import json
    from pathlib import Path
    import httpx
    from sessionquant.macro.analysis import BudgetedAnalyzer
    from test_stage07_audit import company,response,NOW
    repo,other=mysql_repositories(),mysql_repositories()
    repo.record('orders',{'id':'pre-stage07-order','qty':'0.123456'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=response()))) as http:
        result=await BudgetedAnalyzer(repo,api_key='fixture-key',client=http,clock=lambda:NOW).analyze(company(),'TEST',source_kind='rss',primary_verified=True)
    repo.record('news_analysis',result.to_dict())
    repo.record('news_enrichments',{'id':'funnel-fixture','article_id':'unanalysed-fixture',
        'content_hash':'body-hash-v1','document_version':'document-v1',
        'publisher_name':'FixturePublisher','body_char_count':500,'eligible_for_llm':True})
    other.record('news_enrichments',{'id':'other-funnel','article_id':'other-article',
        'content_hash':'other-document','publisher_name':'OtherPublisher','body_char_count':500,'eligible_for_llm':True})
    assert result.llm_call_ids and other.all('llm_calls')==other.all('llm_usage')==[]
    assert repo.all('orders')[0]['id']=='pre-stage07-order'
    with repo.db.cursor() as cursor:
        cursor.execute('SELECT version FROM sq_schema_versions WHERE version=6')
        assert cursor.fetchone()[0]==6
        cursor.execute("SELECT c.payload->>'$.article_id',u.payload->>'$.llm_call_id' FROM pq_llm_calls c "
            "JOIN pq_llm_usage u ON u.namespace=c.namespace AND u.payload->>'$.llm_call_id'=c.payload->>'$.llm_call_id' WHERE c.namespace=%s",(repo.namespace,))
        assert cursor.fetchone()==(company().id,result.llm_call_ids[0])
        text=(Path(__file__).resolve().parents[1]/'sql/llm_call_audit.sql').read_text()
        text='\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('--'))
        results=[]
        for statement in text.replace('REPLACE_WITH_PAPER_NAMESPACE',repo.namespace).split(';'):
            if not statement.strip():continue
            assert statement.lstrip().startswith(('SELECT','WITH'))
            cursor.execute(statement);results.append(cursor.fetchall())
    assert results[0][0][0]=='SUCCEEDED'
    assert results[1][0][0]==result.llm_call_ids[0]
    assert results[4]==results[5]==results[6]==()
    assert results[7]==()
    assert results[8][0][0]=='FixturePublisher' and results[8][0][1:7]==(1,1,1,0,0,0)
    assert len(results[8])==1


def test_real_mysql_stage07_parallel_budget_caps_and_idempotent_settlement(mysql_repositories):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sessionquant.macro.analysis import BudgetLedger,BudgetBlocked
    from sessionquant.macro.models import LLMSettings
    first=mysql_repositories();second=mysql_repositories(first.namespace)
    now=datetime(2026,11,13,16,tzinfo=timezone.utc)
    settings=LLMSettings(hourly_primary_call_cap=1)
    barrier=Barrier(2)
    def reserve(repo):
        barrier.wait()
        try:return asyncio.run(BudgetLedger(repo,settings).reserve('primary',{'messages':[]},now))
        except BudgetBlocked:return None
    with ThreadPoolExecutor(max_workers=2) as pool:reservations=list(pool.map(reserve,[first,second]))
    assert sum(value is not None for value in reservations)==1
    reservation=next(value for value in reservations if value is not None)
    barrier=Barrier(2)
    def settle(repo):
        barrier.wait()
        return asyncio.run(BudgetLedger(repo,settings).settle(reservation,'primary',settings.primary_model,
            input_tokens=100,output_tokens=20,now=now))
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(settle,[first,second]))
    assert len(first.all('llm_usage'))==1
    assert first.all('llm_usage')[0]['cost_usd']=='0.000072'
    assert BudgetLedger(first,settings).snapshot(now)['reserved_usd']=='0'


async def test_real_mysql_stage07_encrypted_expiry_does_not_cross_namespace(mysql_repositories,tmp_path):
    import httpx
    from sessionquant.macro.analysis import BudgetedAnalyzer
    from test_stage07_audit import company,response,NOW
    repo,other=mysql_repositories(),mysql_repositories()
    options={'enabled':True,'key_file':str(tmp_path/'mysql-audit.key'),'retention_days':1,
             'publishers':{'issuer.test':'https://issuer.test/terms'}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=response()))) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture-key',client=http,clock=lambda:NOW,audit_settings=options)
        result=await analyzer.analyze(company(),'TEST',source_kind='rss',primary_verified=True)
    assert repo.all('llm_audit_bodies')
    other.record('llm_audit_bodies',repo.all('llm_audit_bodies')[0])
    assert repo.expire_llm_bodies(NOW+timedelta(days=2),dry_run=True)==1
    assert repo.expire_llm_bodies(NOW+timedelta(days=2))==1
    assert repo.all('llm_audit_bodies')==[] and other.all('llm_audit_bodies')
    assert repo.get_record('llm_calls',result.llm_call_ids[0])['status']=='SUCCEEDED'
    assert len(repo.all('llm_usage'))==1


def test_real_mysql_stage08_selection_intent_sql_and_namespace(mysql_repositories):
    from pathlib import Path
    from test_stage08_selection import market_fixture,batch_for,NOW
    from sessionquant.session_engine import SessionEngine
    from sessionquant.portfolio.selection import VIEW
    repo,other=mysql_repositories(),mysql_repositories()
    repo.record('orders',{'id':'prior-order-stage07','qty':'0.123456'})
    market=market_fixture()
    engine=SessionEngine(repo,market.universe,synthetic=True,candidate_settings={})
    engine.apply_batch(batch_for(market),NOW)
    frame=repo.get_control(VIEW)
    assert frame['approved_new_count']==2 and other.get_control(VIEW) is None
    assert other.all('order_intents')==[]
    assert any(row.get('id')=='prior-order-stage07' for row in repo.all('orders'))
    sql=(Path(__file__).parents[1]/'sql/portfolio_candidate_diagnostics.sql').read_text()
    sql='\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
    results=[]
    with repo.db.cursor() as cursor:
        for statement in sql.replace('REPLACE_WITH_PAPER_NAMESPACE',repo.namespace).split(';'):
            if not statement.strip():continue
            assert statement.lstrip().startswith('SELECT')
            cursor.execute(statement);results.append(cursor.fetchall())
    assert results[0][0][0]==frame['id']
    assert len(results[1])==4 and sum(row[12] is not None for row in results[1])==2
    assert results[2]==results[3]==()


def test_dashboard_ui_commands_are_idempotent_across_mysql_connections(mysql_repositories):
    from sessionquant.trading_controls import begin_session,request_command
    now=datetime.now(timezone.utc)
    first=mysql_repositories();second=mysql_repositories(first.namespace)
    state=begin_session(first,mode='paper',now=now)
    a=request_command(first,'start',run_id=state['run_id'],environment='PAPER_LOCAL',now=now)
    b=request_command(second,'start',run_id=state['run_id'],environment='PAPER_LOCAL',now=now)
    assert a['id']==b['id'] and len(first.all('order_intents'))==1 and first.all('orders')==[]


async def test_missing_cap_subset_metadata_and_policy_survive_mysql_restart(mysql_repositories):
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_config import SessionConfig
    from sessionquant.watchlist.selection import WatchlistManager
    from watchlist_fixtures import NOW, cap_batch, Provider
    repo=mysql_repositories()
    universe=UniverseStore()
    universe.refresh([f'T{i:03}' for i in range(8)],source='SYNTHETIC_FIXTURE',observed_at=NOW-timedelta(days=1))
    batch=cap_batch(8)
    batch['observations']=batch['observations'][4:]
    provider=Provider(batch)
    config=SessionConfig(company_rss_catalog=False,watchlist_size=5,focus_size=5)
    manager=WatchlistManager(repo,universe,config,provider=provider,allow_synthetic=True)
    result=await manager.refresh(NOW)
    assert result['status']=='VALID' and not result['ranking_verified']
    reopened=mysql_repositories(repo.namespace)
    restored=WatchlistManager(reopened,universe,config,provider=provider,allow_synthetic=True)
    status=restored.store.status(NOW,allow_synthetic=True)
    assert status['snapshot']==result['snapshot']
    assert status['excluded_market_cap_symbols']==['T000','T001','T002','T003']
    assert restored.light_symbols(NOW)==['T007','T006','T005','T004']
    strict=WatchlistManager(reopened,universe,replace(config,watchlist_missing_cap_policy='block'),
                            provider=provider,allow_synthetic=True)
    assert strict.entry_blockers('T007',NOW)==['watchlist_partial']


def test_mysql_actual_llm_requests_are_not_hidden_by_prechecks(mysql_repositories):
    from test_news_visibility import call,NOW
    from sessionquant.news.diagnostics import read_news_status
    repo,other=mysql_repositories(),mysql_repositories()
    repo.record('llm_calls',call('success'))
    repo.record('llm_calls',call('failure','EVIDENCE_MISMATCH'))
    with repo.transaction():
        for i in range(510):
            repo.record('llm_calls',call(f'skip-{i}','SKIPPED_PRECHECK',network_attempted=False,
                                        llm_call_id=None,recorded_at=NOW.isoformat()))
    # A string containing "true" is not the verified boolean flag.
    repo.record('llm_calls',call('string-flag',network_attempted='true'))
    other.record('llm_calls',call('other-account'))
    report=read_news_status(repo,NOW)
    assert report['counts']['llm_request_attempts']==2
    assert report['counts']['validated_responses']==report['counts']['failed_or_rejected']==1
    assert [r['id'] for r in repo.recent_llm_attempts(limit=1)]==['failure']
    assert [r['id'] for r in other.recent_llm_attempts()]==['other-account']
    assert not repo.all('orders')


@pytest.mark.parametrize('scenario', [
    'test_quote_checkpoint_bytes_drop_and_restart_retains_full_state',
    'test_version2_migrates_without_rewriting_bars_and_missing_overlay_fails',
    'test_corrected_bar_checkpoint_preserves_versions_and_receipts',
    'test_failed_manifest_write_rolls_back_split_checkpoint_and_poison_engine',
])
def test_mysql_split_market_checkpoint_recovery(mysql_repositories, tmp_path, monkeypatch, scenario):
    """Run the same recovery/rollback contract on real MySQL, not only SQLite."""
    import test_engine_checkpoint_performance as checks
    def repository(*args, **kwargs):
        repo = mysql_repositories()
        # The shared checks close their repository. Keep it open until the fixture
        # has deleted its isolated namespace; monkeypatch restores close first.
        monkeypatch.setattr(repo, 'close', lambda: None)
        return repo
    monkeypatch.setattr(checks, 'Repository', repository)
    check = getattr(checks, scenario)
    if scenario == 'test_corrected_bar_checkpoint_preserves_versions_and_receipts':
        check(tmp_path)
    else:
        check(tmp_path, monkeypatch)


def test_mysql_wide_json_ordered_reads_avoid_filesort_and_preserve_namespace(mysql_repositories, monkeypatch):
    """A bad sort plan can exhaust 32KiB; indexed readers must not sort JSON."""
    import json
    import pymysql
    repo, other = mysql_repositories(), mysql_repositories()
    for i in range(8):
        row = {'id':str(i), 'body':'x' * 262144, 'network_attempted':i % 3 == 0}
        repo.record('news_analysis', row)
        repo.record('llm_calls', row)
        other.record('news_analysis', {**row, 'id':'other-'+str(i)})
        other.record('llm_calls', {**row, 'id':'other-'+str(i)})
    repo.record('llm_calls', {'id':'string-flag', 'body':'x'*262144, 'network_attempted':'true'})
    # In-place updates preserve the original insertion ordering.
    repo.record('news_analysis', {'id':'2', 'body':'y'*262144})
    with repo.db.cursor() as cursor:
        cursor.execute('SET SESSION sort_buffer_size=32768')
        cursor.execute('SET SESSION optimizer_switch="prefer_ordering_index=off"')
        # Reproduce the JSON/filesort failure independent of optimizer statistics.
        # The production query's exact old plan requires its own EXPLAIN evidence.
        with pytest.raises(pymysql.MySQLError) as exc:
            cursor.execute('SELECT payload FROM pq_news_analysis IGNORE INDEX (ix_namespace_seq, PRIMARY) '
                           'WHERE namespace=%s ORDER BY seq DESC LIMIT 10000', (repo.namespace,))
            cursor.fetchall()
        assert exc.value.args[0] == 1038
    captured = []
    original = repo._execute
    def record_query(sql, params=(), **kwargs):
        if sql.startswith('SELECT') and 'ORDER BY seq' in sql:
            captured.append((sql, params))
        return original(sql, params, **kwargs)
    monkeypatch.setattr(repo, '_execute', record_query)
    assert [r['id'] for r in repo.all('news_analysis')] == [str(i) for i in range(8)]
    assert [r['id'] for r in repo.recent('news_analysis', 3)] == ['5','6','7']
    assert [r['id'] for _, r in repo.iter_records('news_analysis', batch_size=3)] == [str(i) for i in range(8)]
    assert [r['id'] for r in repo.recent_llm_attempts(2)] == ['3','6']
    from sessionquant.macro.service import NewsPipeline
    news = NewsPipeline.__new__(NewsPipeline)
    news.repo, news._analyses, news._economic = repo, {}, {}
    news._restore()  # The same restore path used before the engine/feed starts.
    assert set(news._analyses) == {str(i) for i in range(8)}
    def has_filesort(value):
        if isinstance(value, dict):
            return value.get('using_filesort') is True or any(has_filesort(v) for v in value.values())
        return isinstance(value, list) and any(has_filesort(v) for v in value)
    for sql, params in captured:
        with repo.db.cursor() as cursor:
            cursor.execute('EXPLAIN FORMAT=JSON '+sql, params)
            plan = json.loads(cursor.fetchone()[0])
        assert not has_filesort(plan), plan
        assert 'ix_namespace_seq' in json.dumps(plan)
    assert [r['id'] for r in other.recent('news_analysis', 2)] == ['other-6','other-7']


def test_mysql_dashboard_projection_keeps_precision_legacy_orders_and_namespace(mysql_repositories, monkeypatch):
    import json
    from test_dashboard_projection import ledger_fixture, assert_projected_view
    from sessionquant.database import dashboard_projection
    from sessionquant.dashboard.trade_reasons import execution_reasons
    now = datetime(2026, 11, 20, 16, tzinfo=timezone.utc)
    repo, other = mysql_repositories(), mysql_repositories()
    state, fill, _ = ledger_fixture(now)
    repo.save_state(state)
    other.save_state({'environment':'LIVE_LOCAL', 'broker':{'cash':'999', 'positions':{}}})
    sizes = []
    decode = dashboard_projection.decode
    def measured_decode(raw, schema):
        sizes.append(len(raw) if isinstance(raw, (str, bytes)) else len(json.dumps(raw)))
        return decode(raw, schema)
    monkeypatch.setattr(dashboard_projection, 'decode', measured_decode)
    projected = repo.load_dashboard_state()
    order = repo.load_dashboard_order(fill['client_id'])
    assert len(json.dumps(state)) > 2_000_000 and sum(sizes) < 5000
    assert 'orders' not in projected['broker'] and 'fills' not in projected['broker']
    assert projected['synthetic'] is False
    assert_projected_view(state, projected, now)
    projected['broker']['orders'] = {fill['client_id']:order}
    assert execution_reasons([fill], state=projected) == execution_reasons([fill], state=state)
    assert repo.load_dashboard_order('not-present') is None
    assert other.load_dashboard_state()['broker']['cash'] == '999'
    assert other.load_dashboard_order(fill['client_id']) is None
    assert repo.load_state() == state
    recorded = {'client_id':fill['client_id'], 'symbol':'MSFT', 'side':'sell',
                'reason':'manual_market_sell', 'unused':'x'*100_000}
    repo.record('orders', recorded)
    assert repo.load_dashboard_order(fill['client_id'], recovery_state=False) == {
        key:value for key,value in recorded.items() if key != 'unused'}
    assert other.load_dashboard_order(fill['client_id'], recovery_state=False) is None
    legacy = {'environment':'LIVE_LOCAL', 'account':{'cash':'10.00000000000000000001',
              'equity':'10.00000000000000000001', 'reserved_cash':None, 'positions':{}}}
    repo.save_state(legacy)
    assert repo.load_dashboard_state() == legacy


@pytest.mark.parametrize('fail_write', [False, True])
async def test_mysql_history_reconciliation_is_atomic(mysql_repositories, monkeypatch, fail_write):
    from sessionquant.market.history import HistoryStore, KIND, history_plan
    from sessionquant.market.history_repair import repair_conflict
    from sessionquant.watchlist.budget import ReadCallBudget
    from test_watchlist_history import NOW, Pages, minute_rows
    repo = mysql_repositories()
    store = HistoryStore(repo)
    day, opened, closed = history_plan(NOW)[-1]
    bars = minute_rows('AAPL', opened)
    old = store.save('AAPL', day, bars+[replace(bars[0], volume=999)])
    with repo.db.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM pq_controls WHERE namespace=%s', (repo.namespace,))
        before_count = cur.fetchone()[0]
    if fail_write:
        def fail(*a, **kw):
            raise RuntimeError('injected write failure')
        monkeypatch.setattr(repo, 'record', fail)
        with pytest.raises(RuntimeError):
            await repair_conflict(store, 'AAPL', day, opened, closed, Pages(bars), ReadCallBudget(4), now=NOW)
    else:
        result = await repair_conflict(store, 'AAPL', day, opened, closed, Pages(bars), ReadCallBudget(4), now=NOW)
        assert result['status']=='REPAIRED'
    reopened = mysql_repositories(repo.config.namespace, environment=repo.config.environment)
    row = reopened.get_record(KIND, old['id'])
    if fail_write:
        assert row == old
        with reopened.db.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM pq_controls WHERE namespace=%s', (repo.namespace,))
            assert cur.fetchone()[0] == before_count
    else:
        assert row['quality']['complete']
        audit = reopened.get_control(result['audit_control'])
        assert audit['before']==old and audit['after']==row
    assert not reopened.all('orders')


def test_mysql_auxiliary_budget_survives_restart_and_isolates_ledgers(mysql_repositories):
    from sessionquant.research.storage import BUDGET, StoragePolicy, store
    repo, other = mysql_repositories(), mysql_repositories()
    now = datetime(2026,11,24,16,tzinfo=timezone.utc)
    policy = StoragePolicy(mode='bounded_diagnostics',daily_records=1)
    assert store(repo,policy,'research_events',{'id':'sample'},'frame',now)
    reopened = mysql_repositories(repo.config.namespace,environment=repo.config.environment)
    assert not store(reopened,policy,'research_events',{'id':'over-budget'},'frame',now,urgent=True)
    assert len(reopened.all('research_events'))==1
    assert reopened.get_control(BUDGET)['records']==1
    assert store(other,policy,'research_events',{'id':'sample'},'frame',now)
    with pytest.raises(RuntimeError):
        with reopened.transaction():
            assert store(reopened,policy,'research_events',{'id':'rollback'},'frame',now+timedelta(days=1))
            raise RuntimeError('rollback')
    assert reopened.get_record('research_events','rollback') is None
    assert reopened.get_control(BUDGET)['day']=='2026-11-24'


def test_mysql_system_summary_bounds_payloads_and_isolates_namespaces(mysql_repositories):
    from test_pipeline_projection import assert_projection
    repo,other=mysql_repositories(),mysql_repositories()
    assert_projection(repo)
    assert other.recent_pipeline_summary()==[]
