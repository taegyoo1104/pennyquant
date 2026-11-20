"""Home refreshes transfer current holdings and bases, not lifetime recovery history."""
from contextlib import closing
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.home_returns import account_returns
from sessionquant.dashboard.session_app import DashboardData, build_view, load_database
from sessionquant.dashboard.trade_reasons import execution_reasons
from sessionquant.database.repository import Repository


def ledger_fixture(now, count=1000):
    order_id = 'old-"quoted"-\\order'
    state = {'environment':'PAPER_LOCAL', 'synthetic':False, 'strategy':'pullback_simple',
        'last_time':now.isoformat(), 'unused_history':['x'*500]*count,
        'broker':{'cash':'94', 'equity':'104', 'reserved_cash':'0', 'realized_pnl':'4',
            'day_start_equity':'102', 'session_date':now.astimezone(ZoneInfo('America/New_York')).date().isoformat(),
            'settings':{'initial_cash':'100', 'max_positions':3, 'preferred_positions':3},
            'positions':{'AAPL':{'qty':'0.10000000000000000001', 'avg_price':'90', 'entry_fee':'0.01'}},
            'exit_intents':{'AAPL':{'reason':'stop_loss'}},
            'orders':{str(i):{'unused':'x'*500} for i in range(count)},
            'fills':[{'unused':'x'*500}]*count, 'alerts':[{'unused':'x'*500}]*count}}
    order = {'intent':{'client_id':order_id, 'symbol':'MSFT', 'side':'sell', 'reason':'stop_loss'},
             'unused':'x'*500}
    state['broker']['orders'][order_id] = order
    fill = {'id':'latest-fill', 'client_id':order_id, 'symbol':'MSFT', 'side':'sell', 'timestamp':now.isoformat()}
    return state, fill, order


def assert_projected_view(full, projected, now):
    quotes = [{'symbol':'AAPL', 'bid':'100', 'timestamp':now.isoformat(), 'received_at':now.isoformat()}]
    before, after = DashboardData(state=full), DashboardData(state=projected)
    before.tables['quote_snapshots'] = after.tables['quote_snapshots'] = quotes
    original, compact = build_view(before, now), build_view(after, now)
    for key in ('positions', 'environment', 'synthetic', 'last_event', 'regime_label'):
        assert original[key] == compact[key]
    assert account_returns(full, original['account'], now) == account_returns(projected, compact['account'], now)


def test_home_projection_does_not_transfer_lifetime_payload_and_keeps_display(tmp_path, monkeypatch):
    from sessionquant.database import dashboard_projection
    now = datetime.now(timezone.utc)
    state, fill, order = ledger_fixture(now)
    path = tmp_path/'projection.db'
    with closing(Repository(path, backend='sqlite')) as repo:
        repo.save_state(state)
        repo.import_record('executions', fill, fill['id'])
    sizes = []
    decode = dashboard_projection.decode
    def measured_decode(raw, schema):
        sizes.append(len(raw))
        return decode(raw, schema)
    monkeypatch.setattr(dashboard_projection, 'decode', measured_decode)
    monkeypatch.setattr(Repository, 'load_state', lambda self: pytest.fail('home read the full recovery ledger'))
    data = load_database(path, page='대시보드', panel='live')
    assert data.connected and data.error is None
    assert len(json.dumps(state)) > 2_000_000 and sum(sizes) < 5000
    assert 'fills' not in data.state['broker'] and len(data.state['broker']['orders']) == 1
    assert_projected_view(state, data.state, now)
    assert execution_reasons([fill], state=data.state) == execution_reasons([fill], state=state)
    assert data.state['broker']['orders'][fill['client_id']]['intent'] == order['intent']


@pytest.mark.parametrize('container', ['broker', 'portfolio', None])
@pytest.mark.parametrize('nested_account', [False, True])
def test_projection_keeps_legacy_account_fallback_and_explicit_nulls(tmp_path, container, nested_account):
    now = datetime.now(timezone.utc)
    account = {'cash':'10.00000000000000000001', 'equity':'10.00000000000000000001',
               'positions':{}, 'reserved_cash':None}
    body = {'account':account} if nested_account else account
    state = {container:body} if container else body
    state = {**state, 'synthetic':False, 'environment':'LIVE_LOCAL'}
    with closing(Repository(tmp_path/'legacy.db', backend='sqlite')) as repo:
        repo.save_state(state)
        projected = repo.load_dashboard_state()
        assert projected == state and projected['synthetic'] is False
        assert_projected_view(state, projected, now)
        assert repo.load_state() == state  # Recovery storage remains unchanged.


def test_home_render_and_controls_do_not_reread_recovery_state(tmp_path, monkeypatch):
    from sessionquant.trading_controls import begin_session
    now = datetime.now(timezone.utc)
    state, fill, _ = ledger_fixture(now, count=20)
    path = tmp_path/'home.db'
    with closing(Repository(path, backend='sqlite')) as repo:
        repo.save_state(state)
        repo.import_record('executions', fill, fill['id'])
        repo.import_record('quote_snapshots', {'symbol':'AAPL', 'bid':'100', 'timestamp':now.isoformat()}, 'AAPL')
        begin_session(repo, mode='paper', now=now)
    monkeypatch.setattr(Repository, 'load_state', lambda self: pytest.fail('rendering read full recovery state'))
    script = ('from sessionquant.dashboard.session_app import render_dashboard\n'
              f'render_dashboard({str(path)!r}, selected_page="대시보드", panel="live")')
    app = AppTest.from_string(script, default_timeout=30).run()
    assert not app.exception and not app.error
    values = {row.label:row.value for row in app.metric}
    assert values['모의 총자산'] == '$104.00' and values['일간 수익률'] == '+1.96%'
    assert values['총 수익률'] == '+4.00%'
    assert any('매도 이유: 손절 기준 가격 도달' in row.value for row in app.caption)
    assert any('AAPL 0.10000000000000000001주' in row.value for row in app.caption)
    assert any('AAPL' in row.value and '평가 손익' in row.value for row in app.markdown)
    with closing(Repository(path, backend='sqlite')) as repo:
        assert not repo.all('order_intents') and not repo.all('orders')


def test_order_projection_keeps_conflicting_record_reason_and_skips_resolved_fills(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    state, fill, _ = ledger_fixture(now, count=1)
    path = tmp_path/'order-sources.db'
    with closing(Repository(path, backend='sqlite')) as repo:
        repo.save_state(state)
        repo.import_record('executions', fill, fill['id'])
        repo.import_record('orders', {'client_id':fill['client_id'], 'symbol':'MSFT', 'side':'sell',
            'reason':'manual_market_sell', 'unused':'x'*100_000}, fill['client_id'])
    data = load_database(path, page='대시보드', panel='live')
    assert data.connected and len(json.dumps(data.tables['orders'])) < 500
    # Conflicting stored causes remain unknown instead of choosing one source.
    assert execution_reasons([fill], state=data.state, orders=data.tables['orders'])[0]['매도 이유'] == '기록 없음'
    with closing(Repository(path, backend='sqlite')) as repo:
        repo.import_record('executions', {**fill, 'reason':'session_exit'}, fill['id'])
    monkeypatch.setattr(Repository, 'load_dashboard_order', lambda *a, **kw: pytest.fail('fill already carries its reason'))
    data = load_database(path, page='대시보드', panel='live')
    assert data.connected and not data.error
    assert execution_reasons(data.tables['executions'], state=data.state)[0]['매도 이유'] == '장 마감 전 정리'
