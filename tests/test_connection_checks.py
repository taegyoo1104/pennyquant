"""전체 접속 점검의 오프라인 동작·인증 충돌 회피·비밀정보 보호를 검증한다."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import httpx
import pytest

from sessionquant.connection_checks import active_collector, check_connections
from sessionquant.database.repository import Repository


def config(tmp_path):
    path = tmp_path/'session.yaml'
    path.write_text('database_backend: sqlite\ndb_path: inspect.db\ncompanies: {}\n')
    return path


async def test_offline_all_check_makes_no_network_or_database_calls(tmp_path, monkeypatch):
    monkeypatch.setenv('TOSS_CLIENT_ID', 'private-id')
    monkeypatch.setenv('TOSS_CLIENT_SECRET', 'private-secret')
    monkeypatch.setattr(httpx.AsyncClient, 'request', lambda *a, **k: pytest.fail('오프라인 네트워크 호출'))
    monkeypatch.setattr('sessionquant.database.repository.Repository', lambda *a, **k: pytest.fail('오프라인 DB 호출'))
    result = await check_connections(config(tmp_path), offline=True)
    assert result['toss']['client_id_configured']
    assert result['mysql']['status'] == 'not_tested'
    assert result['order_writes'] == result['llm_generation_calls'] == 0
    assert not result['llm_credit_balance_verified']
    assert 'private' not in json.dumps(result)


async def test_active_engine_skips_new_toss_token_and_db_failure_also_skips(tmp_path, monkeypatch):
    import sessionquant.connection_checks as module
    async def news(*a, **k): return {'connections': {}}
    monkeypatch.setattr(module, 'check_news', news)
    monkeypatch.setattr('sessionquant.collectors.toss_market.TossMarketClient', lambda: pytest.fail('인증 충돌 가능 토큰 재발급'))
    settings = config(tmp_path)
    repo = Repository(tmp_path/'inspect.db', backend='sqlite')
    repo.set_control('trade_control_state', {'armed': True, 'heartbeat': datetime.now(timezone.utc).isoformat()})
    repo.close()
    result = await check_connections(settings)
    assert result['mysql']['status'] == 'ok'
    assert result['toss']['status'] == 'skipped_active_collector'
    def broken(*a, **k): raise RuntimeError('password=private-db-password')
    monkeypatch.setattr('sessionquant.database.repository.Repository', broken)
    result = await check_connections(settings)
    assert result['mysql']['status'] == 'error'
    assert result['toss']['status'] == 'skipped_database_unavailable'
    assert 'private-db-password' not in json.dumps(result)


def test_mysql_active_collector_searches_all_runtime_namespaces():
    now = datetime.now(timezone.utc).isoformat()
    def execute(sql, params, fetch):
        assert 'WHERE name IN' in sql and 'namespace=' not in sql
        return [('account_monitor_status', json.dumps({'status': 'ok', 'updated_at': now}))]
    assert active_collector(SimpleNamespace(backend='mysql', _execute=execute))


async def test_all_check_uses_only_market_and_account_reads(tmp_path, monkeypatch):
    import sessionquant.connection_checks as module
    async def news(*a, **k): return {}
    monkeypatch.setattr(module, 'check_news', news)
    monkeypatch.setenv('TOSS_CLIENT_ID', 'private-id')
    monkeypatch.setenv('TOSS_CLIENT_SECRET', 'private-secret')
    calls = []
    class Client:
        async def prices(self, symbols): calls.append(('prices', symbols))
        async def aclose(self): calls.append(('closed', None))
    async def account(client):
        calls.append(('account', None))
        return {'issues': []}
    monkeypatch.setattr('sessionquant.collectors.toss_market.TossMarketClient', Client)
    monkeypatch.setattr('sessionquant.account_monitor.fetch_account_snapshot', account)
    result = await check_connections(config(tmp_path))
    assert calls == [('prices', ['AAPL']), ('account', None), ('closed', None)]
    assert result['toss']['status'] == 'ok'
    assert not result['toss']['order_permissions_verified']
    assert not result['llm_credit_balance_verified']
