"""The existing ledger is retained; only an offline, flat approved profile can change."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from sessionquant.database.repository import Repository
from sessionquant.portfolio.broker import PaperBroker
from sessionquant.portfolio.models import PortfolioSettings, serialize
from sessionquant.portfolio.position_limit_upgrade import upgrade, UPGRADE_ID

SETTINGS = PortfolioSettings(preferred_positions=3, max_positions=3)


def fixture(repo):
    state = {'schema_version':3, 'environment':'PAPER_LOCAL', 'strategy':'pullback_simple',
        'synthetic':False, 'paper_data_policy':'observed_paper_v1',
        'consumed_signals':['already-ordered'], 'broker':PaperBroker().to_state()}
    state['broker'].update(cash='100.12', realized_pnl='0.12')
    repo.save_state(state)
    repo.set_control('trade_control_state', {'mode':'paper', 'status':'OFFLINE', 'armed':False, 'running':False})
    return state


def test_flat_upgrade_preserves_money_history_and_signal_ids_and_is_idempotent(tmp_path):
    with closing(Repository(tmp_path/'upgrade.db')) as repo:
        original = fixture(repo)
        repo.record('executions', {'id':'historical-fill', 'symbol':'AAPL', 'qty':'0.2'})
        assert upgrade(repo, SETTINGS)
        expected = deepcopy(original)
        expected['broker']['settings'] = serialize(SETTINGS)
        assert repo.load_state() == expected
        assert len(repo.all('executions')) == 1
        assert repo.all('schema_migrations')[0]['id'] == UPGRADE_ID
        assert PaperBroker.from_state(repo.load_state()['broker'], SETTINGS).cash == Decimal('100.12')
        assert not upgrade(repo, SETTINGS)
        assert len(repo.all('schema_migrations')) == 1
        with pytest.raises(ValueError, match='재시작 설정'):
            PaperBroker.from_state(repo.load_state()['broker'], PortfolioSettings())


@pytest.mark.parametrize('issue', ['running', 'armed', 'waiting', 'live', 'other_strategy',
    'synthetic', 'other_settings', 'pending_command', 'position', 'pending_order', 'exit', 'missing_positions'])
def test_active_unknown_or_different_policy_is_never_overwritten(tmp_path, issue):
    from test_paper_market_fills import bought, intent
    with closing(Repository(tmp_path/'blocked.db')) as repo:
        state = fixture(repo)
        control = repo.get_control('trade_control_state')
        if issue == 'running': control['running'] = True
        if issue == 'armed': control['armed'] = True
        if issue == 'waiting': control['status'] = 'WAITING'
        if issue == 'live': state['environment'] = 'LIVE_LOCAL'
        if issue == 'other_strategy': state['strategy'] = 'pullback'
        if issue == 'synthetic': state['synthetic'] = True
        if issue == 'other_settings': state['broker']['settings']['initial_cash'] = '200'
        if issue == 'position': state['broker'] = bought().to_state()
        if issue == 'pending_order':
            broker = PaperBroker(); broker.submit_order(intent())
            state['broker'] = broker.to_state()
        if issue == 'exit': state['broker']['exit_intents'] = {'AAPL':{'reason':'manual'}}
        if issue == 'missing_positions': del state['broker']['positions']
        if issue == 'pending_command':
            repo.record('order_intents', {'id':'pending', 'kind':'dashboard_command', 'status':'pending'})
        repo.save_state(state)
        repo.set_control('trade_control_state', control)
        with pytest.raises(ValueError):
            upgrade(repo, SETTINGS)
        assert repo.load_state() == state and not repo.all('schema_migrations')


def test_audit_failure_rolls_back_settings_and_empty_ledger_is_untouched(tmp_path, monkeypatch):
    with closing(Repository(tmp_path/'atomic.db')) as repo:
        assert not upgrade(repo, SETTINGS)
        before = fixture(repo)
        def fail(*args): raise RuntimeError('audit failed')
        monkeypatch.setattr(repo, 'record', fail)
        with pytest.raises(RuntimeError): upgrade(repo, SETTINGS)
        assert repo.load_state() == before
        with pytest.raises(ValueError): upgrade(repo, replace(SETTINGS, max_positions=4))


def test_maintenance_entrypoint_loads_active_profile_and_keeps_the_same_database(tmp_path, monkeypatch):
    import yaml
    from sessionquant.portfolio.position_limit_upgrade import main
    path = tmp_path/'ledger.db'
    profile = tmp_path/'sessionquant.strategy-lab-websocket.example.yaml'
    profile.write_text(yaml.safe_dump({'environment':'PAPER_LOCAL', 'database_backend':'sqlite',
        'db_path':str(path), 'strategy':'pullback_simple', 'portfolio':serialize(SETTINGS)}))
    with closing(Repository(path)) as repo:
        fixture(repo)
    monkeypatch.setenv('SQ_SESSION_CONFIG', str(profile))
    monkeypatch.setenv('SQ_ENVIRONMENT', 'PAPER_LOCAL')
    main()
    with closing(Repository(path, initialize=False)) as repo:
        assert repo.load_state()['broker']['settings']['max_positions'] == 3
        assert repo.load_state()['broker']['cash'] == '100.12'
