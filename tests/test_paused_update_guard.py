"""Intraday maintenance requires an explicit stopped, flat Paper ledger."""
import importlib.util
from pathlib import Path

import pytest


def guard():
    path = Path(__file__).parents[1] / 'deploy/gcp/update_guard.py'
    spec = importlib.util.spec_from_file_location('update_guard', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('status', ['WAITING', 'STOPPED'])
def test_paused_flat_paper_is_allowed(status):
    guard().verify({'mode': 'paper', 'running': False, 'status': status},
                   {'environment': 'PAPER_LOCAL', 'broker': {'orders': {'o': {'status': 'filled'}},
                    'positions': {}, 'exit_intents': {}}}, 0)


@pytest.mark.parametrize('issue', ['running', 'liquidating', 'live', 'positions', 'exit', 'order',
                                 'pending', 'missing_ledger', 'missing_orders'])
def test_unknown_or_active_state_blocks_update(issue):
    control = {'mode': 'paper', 'running': False, 'status': 'STOPPED'}
    state = {'environment': 'PAPER_LOCAL', 'broker': {'orders': {}, 'positions': {}, 'exit_intents': {}}}
    if issue == 'running': control['running'] = True
    if issue == 'liquidating': control['status'] = 'LIQUIDATING'
    if issue == 'live': control['mode'] = 'live'
    if issue == 'positions': state['broker']['positions'] = {'AAPL': {'qty': '1'}}
    if issue == 'exit': state['broker']['exit_intents'] = {'AAPL': {}}
    if issue == 'order': state['broker']['orders'] = {'o': {'status': 'unknown'}}
    if issue == 'missing_ledger': state = {}
    if issue == 'missing_orders': del state['broker']['orders']
    with pytest.raises(ValueError):
        guard().verify(control, state, int(issue == 'pending'))


def test_real_finish_session_lifecycle_requires_poststop_mode():
    from sessionquant.trading_controls import finish_session
    from types import SimpleNamespace
    saved = {}
    repo = SimpleNamespace(set_control=lambda key, value: saved.update({key: dict(value)}))
    control = {'mode': 'paper', 'running': False, 'status': 'STOPPED', 'armed': True}
    state = {'environment': 'PAPER_LOCAL', 'broker': {'orders': {}, 'positions': {}, 'exit_intents': {}}}
    check = guard().verify
    check(control, state, 0)
    finish_session(repo, control)
    offline = saved['trade_control_state']
    with pytest.raises(ValueError): check(offline, state, 0)
    check(offline, state, 0, stopped=True)
    with pytest.raises(ValueError): check({**offline, 'armed': True}, state, 0, stopped=True)
    with pytest.raises(ValueError): check(offline, state, 1, stopped=True)
