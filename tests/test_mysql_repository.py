from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sys
from types import SimpleNamespace

import pytest

from sessionquant.database.config import MySQLConfig
from sessionquant.database.mysql_repository import MySQLRepository


class DriverError(Exception):
    pass


def test_mysql_timing_records_counts_without_sql_or_credentials(connection):
    from sessionquant.performance import capture
    repo=repository(connection)
    try:
        with capture() as timings:
            with repo.transaction():repo.set_control('PRIVATE',{'secret':'PRIVATE'})
        rows={row['phase']:row for row in timings.snapshot()['phases']}
        assert rows['db_query']['calls']>=2 and rows['db_commit']['calls']==1
        assert rows['json_encode']['calls']>=1 and 'PRIVATE' not in str(rows)
    finally:repo.close()


class FakeConnection:
    def __init__(self):
        self.version = "8.0.46"
        self.calls = []
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.lock_result = 1
        self.delete_counts = []
        self.failure = None
        self.failure_code = None
        self.commit_failure = False

    @contextmanager
    def cursor(self):
        connection = self
        class Cursor:
            rowcount = 0
            def execute(self, sql, params=()):
                connection.calls.append((sql, params))
                self.sql = sql
                if connection.failure and connection.failure in sql:
                    if connection.failure_code is not None:
                        raise DriverError(connection.failure_code,"PRIVATE password host SQL payload")
                    raise DriverError("PRIVATE password host")
                self.rowcount = connection.delete_counts.pop(0) if sql.startswith("DELETE") and connection.delete_counts else 1
            def fetchone(self):
                if self.sql.startswith("SELECT VERSION"):
                    return (connection.version,)
                if "GET_LOCK" in self.sql:
                    return (connection.lock_result,)
                if "COUNT(*)" in self.sql:
                    return (3, 300) if "JSON_STORAGE_SIZE" in self.sql else (3,)
                return None
            def fetchall(self):
                return []
        yield Cursor()

    def commit(self):
        self.commits += 1
        if self.commit_failure:
            raise DriverError("PRIVATE commit password")

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def connection(monkeypatch):
    conn = FakeConnection()
    driver = SimpleNamespace(MySQLError=DriverError, connect=lambda **kwargs: conn)
    monkeypatch.setitem(sys.modules, "pymysql", driver)
    return conn


def repository(connection, **updates):
    return MySQLRepository("data/system.db", MySQLConfig(password="PRIVATE", **updates))


@pytest.mark.parametrize("version", ["5.7.44", "10.11.10-MariaDB", "unverified"])
def test_requires_verified_mysql_eight_and_closes_failed_initialization(connection, version):
    connection.version = version
    with pytest.raises(RuntimeError, match="MySQL 8"):
        repository(connection)
    assert connection.closed


def test_record_uses_hashed_identity_and_bound_json_without_sql_interpolation(connection):
    repo = repository(connection)
    record = {"id": "long:" + "x" * 1000, "title": "한글 quote ' ; DROP TABLE pq_orders"}
    repo.record("news_analysis", record)
    # 연구 기록 상태 조회가 뒤따르므로 검증할 원본 INSERT를 명시적으로 선택한다.
    inserts = [(sql,params) for sql,params in connection.calls if sql.startswith("INSERT INTO pq_news_analysis(")]
    assert len(inserts) == 1
    sql,params = inserts[0]
    assert "DROP TABLE" not in sql and len(params[2]) == 32
    assert json.loads(params[3]) == record and params[0] == "system"
    before = len(connection.calls)
    with pytest.raises(ValueError):
        repo.record("orders;DROP TABLE", record)
    assert len(connection.calls) == before


def test_nested_engine_transaction_commits_once_and_rolls_back_failure(connection):
    repo = repository(connection)
    with repo.transaction():
        repo.save_state({"account": {"cash": 70}})
        with repo.transaction():
            repo.set_control("manual_halt", False)
    assert connection.commits == 1
    assert sum(sql == "START TRANSACTION" for sql, _ in connection.calls) == 1
    with pytest.raises(ValueError):
        with repo.transaction():
            raise ValueError("A caller validation failure")
    assert connection.rollbacks == 1 and not repo._in_transaction


def test_transaction_commit_failure_does_not_echo_driver_details(connection):
    repo = repository(connection)
    connection.commit_failure = True
    with pytest.raises(RuntimeError) as exc:
        with repo.transaction():
            repo.save_state({"account": {"cash": 70}})
    assert "PRIVATE" not in str(exc.value)
    assert connection.rollbacks == 1 and not repo._in_transaction


def test_connection_and_operation_errors_are_sanitized(connection, monkeypatch):
    repo = repository(connection)
    connection.failure = "INSERT INTO"
    with pytest.raises(RuntimeError) as exc:
        repo.save_state({"cash": 70})
    assert "PRIVATE" not in str(exc.value)
    def fail(**kwargs):
        raise DriverError("PRIVATE user host password")
    monkeypatch.setitem(sys.modules, "pymysql", SimpleNamespace(MySQLError=DriverError, connect=fail))
    with pytest.raises(RuntimeError) as exc:
        repository(connection)
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.parametrize('code',[1142,1153,1205,1213,2006,2013])
def test_driver_code_is_reported_without_query_or_secrets(connection,caplog,code):
    repo=repository(connection)
    connection.failure='INSERT INTO'
    connection.failure_code=code
    with pytest.raises(RuntimeError) as exc:
        repo.save_state({'secret':'PRIVATE ledger contents'})
    assert f'코드 {code}' in str(exc.value) and '작업 INSERT' in str(exc.value)
    assert f'코드 {code}' in caplog.text
    assert 'PRIVATE' not in str(exc.value)+caplog.text
    assert 'pq_engine_state' not in str(exc.value)+caplog.text


@pytest.mark.parametrize('code',['PRIVATE password',True,None,-1,70000])
def test_non_numeric_or_invalid_driver_codes_are_not_echoed(code):
    from sessionquant.database.mysql_repository import mysql_failure_message
    message=mysql_failure_message(DriverError(code,'PRIVATE server details'),'PRIVATE SQL')
    assert '코드 미확인' in message and '작업 QUERY' in message and 'PRIVATE' not in message


def test_connect_code_distinguishes_authentication_failure(connection,monkeypatch):
    def fail(**kwargs):
        raise DriverError(1045,'PRIVATE username and password')
    monkeypatch.setitem(sys.modules,'pymysql',SimpleNamespace(MySQLError=DriverError,connect=fail))
    with pytest.raises(RuntimeError) as exc:
        repository(connection)
    assert '코드 1045' in str(exc.value) and '작업 CONNECT' in str(exc.value)
    assert 'PRIVATE' not in str(exc.value)


def test_server_writer_lease_blocks_competitor_and_releases_after_failure(connection):
    repo = repository(connection)
    with pytest.raises(ValueError):
        with repo.engine_lease():
            with repo.engine_lease():
                raise ValueError("caller failure")
    queries = [sql for sql, _ in connection.calls]
    assert sum("GET_LOCK" in sql for sql in queries) == 1
    assert sum("RELEASE_LOCK" in sql for sql in queries) == 1
    connection.lock_result = 0
    with pytest.raises(RuntimeError, match="다른 엔진"):
        with repo.engine_lease():
            pytest.fail("Competing writer must not acquire a lease")


def test_retention_defaults_to_preview_and_cannot_delete_financial_state(connection):
    repo = repository(connection)
    before = datetime(2026, 10, 1, tzinfo=timezone.utc)
    preview = repo.prune(before, kinds=("price_samples",))
    assert preview == {"price_samples": 3}
    assert not any(sql.startswith("DELETE") for sql, _ in connection.calls)
    for kind in ("orders", "executions", "positions", "alerts", "controls", "engine_state"):
        with pytest.raises(ValueError, match="observational"):
            repo.prune(before, kinds=(kind,), dry_run=False)
    connection.delete_counts = [2, 1]
    assert repo.prune(before, kinds=("price_samples",), batch_size=2, dry_run=False) == {"price_samples": 3}
    deletes = [(sql, params) for sql, params in connection.calls if sql.startswith("DELETE")]
    assert len(deletes) == 2 and all(params[0] == "system" for _, params in deletes)


def test_retention_refuses_engine_transaction_and_naive_cutoff(connection):
    repo = repository(connection)
    with pytest.raises(ValueError, match="timezone-aware"):
        repo.prune(datetime(2026, 10, 1))
    with repo.transaction():
        with pytest.raises(ValueError, match="outside"):
            repo.prune(datetime(2026, 10, 1, tzinfo=timezone.utc), dry_run=False)


def test_size_report_distinguishes_namespace_payload_from_physical_table_scope(connection):
    repo = repository(connection)
    report = repo.storage_stats()
    assert report["namespace"] == "system" and report["namespace_rows"]["orders"] == 3
    assert report["namespace_payload_bytes"] > 0
    assert "all namespaces" in report["physical_scope"]
