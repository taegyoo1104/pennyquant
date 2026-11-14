"""가설 위험 신호의 OFF/합성 ON·회복·청산 독립성을 검증한다."""
from datetime import timedelta

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.universe import UniverseStore
from sessionquant.market_reference.contracts import REFERENCES
from sessionquant.market_reference.risk import MacroReferenceRiskMonitor, filter_settings, STATUS
from sessionquant.market_reference.store import VIEW
from sessionquant.models import jsonable
from sessionquant.session_engine import SessionEngine, event_time
from sessionquant.session_demo import synthetic_tape
from test_market_reference import NOW
from test_paper_market_fills import intent, quote


def view(*, risk=False, down=False, at=NOW):
    """검증 플래그는 합성 픽스처에만 사용한다. 실제 권한 증거가 아니다."""
    result = {'items': {}}
    for symbol in REFERENCES:
        windows = {}
        for minutes in (1, 5, 15, 30):
            value = '-.4' if risk and symbol == 'IEF' else '-.1' if down and symbol in {'SPY', 'QQQ'} else '0'
            windows[str(minutes)] = {'pct_change': value, 'volatility_pct': '.01', 'quality_status': 'GOOD',
                'sample_count': minutes+1, 'required_samples': minutes+1,
                'start_at': (at-timedelta(minutes=minutes)).isoformat(), 'event_at': at.isoformat(),
                'available_at': at.isoformat(), 'input_ids': [f'{symbol}:{at}:{i}' for i in range(minutes+1)]}
        result['items'][symbol] = {'id': f'{symbol}:{at}:{risk}:{down}', 'symbol': symbol, 'observed_at': at.isoformat(),
            'event_at': at.isoformat(), 'available_at': at.isoformat(), 'valid_until': (at+timedelta(seconds=300)).isoformat(),
            'latency_class': 'REALTIME', 'quality_status': 'GOOD', 'windows': windows, 'synthetic': True}
    return result


@pytest.fixture
def repo(tmp_path):
    database = Repository(tmp_path/'risk.db', backend='sqlite')
    yield database
    database.close()


def active(repo, **kwargs):
    return MacroReferenceRiskMonitor(repo, {'enabled': True, 'paper_shadow_only': False, **kwargs}, research_only=True)


@pytest.mark.parametrize('risk,down,status', [(False,False,'NORMAL'), (True,False,'WATCH'), (True,True,'ELEVATED')])
def test_verified_comovement_and_off_shadow_do_not_change_trades(repo, risk, down, status):
    monitor = MacroReferenceRiskMonitor(repo)
    event = monitor.evaluate(NOW, view=view(risk=risk, down=down))
    assert event['status'] == status and not event['enabled'] and not event['block_entries']
    assert event['force_exit'] is False and event['quality'] == 'VERIFIED'
    assert event['triggering_measurements']['USO']['windows']['30']['sample_count'] == 31
    assert repo.all('orders') == []


@pytest.mark.parametrize('case', ['delayed', 'stale', 'missing', 'unknown_latency', 'future_receipt',
    'short_sample', 'duplicate_sample', 'mixed_end', 'wrong_duration', 'nan', 'malformed'])
def test_active_fail_closed_for_unverified_data_never_forces_exit(repo, case):
    data = view()
    item = data['items']['IEF']; window = item['windows']['15']
    if case == 'delayed': item['latency_class'] = 'DELAYED'
    if case == 'stale': item['valid_until'] = (NOW-timedelta(seconds=1)).isoformat()
    if case == 'missing': del data['items']['IEF']
    if case == 'unknown_latency': item['latency_class'] = 'UNKNOWN'
    if case == 'future_receipt': item['available_at'] = (NOW+timedelta(seconds=1)).isoformat()
    if case == 'short_sample': window['sample_count'] = 15
    if case == 'duplicate_sample': window['input_ids'] = ['one']*16
    if case == 'mixed_end':
        window['event_at'] = (NOW-timedelta(minutes=1)).isoformat()
        window['start_at'] = (NOW-timedelta(minutes=16)).isoformat()
    if case == 'wrong_duration': window['start_at'] = NOW.isoformat()
    if case == 'nan': window['pct_change'] = 'NaN'
    if case == 'malformed': data['items'] = []
    event = active(repo).evaluate(NOW, view=data)
    assert event['status'] == 'UNKNOWN' and event['block_entries'] and event['block_adds']
    assert not event['force_exit'] and event['quality_reasons']


def test_explicit_unknown_policy_and_shadow_flag(repo):
    assert not active(repo, fail_closed=False).evaluate(NOW, view={})['block_entries']
    shadow = MacroReferenceRiskMonitor(repo, {'enabled': True, 'paper_shadow_only': True})
    assert not shadow.evaluate(NOW, view=view(risk=True))['block_entries']
    assert shadow.evaluate(NOW, view={})['status'] == 'UNKNOWN'


def test_unique_normal_frames_cooldown_and_restart_preserve_risk_latch(repo):
    monitor = active(repo, cooldown_seconds=60)
    first = monitor.evaluate(NOW, view=view(risk=True))
    assert first['block_entries']
    for _ in range(4):
        normal = monitor.evaluate(NOW+timedelta(seconds=20), view=view(at=NOW+timedelta(seconds=20)))
        assert normal['status'] == 'WATCH' and normal['normal_sample_count'] == 1
    restored = active(repo, cooldown_seconds=60)
    second = restored.evaluate(NOW+timedelta(seconds=40), view=view(at=NOW+timedelta(seconds=40)))
    assert second['normal_sample_count'] == 2 and second['block_entries']
    last = restored.evaluate(NOW+timedelta(seconds=60), view=view(at=NOW+timedelta(seconds=60)))
    assert last['status'] == 'NORMAL' and not last['block_entries']
    assert last['clear_reason'] == 'verified_normal_samples_and_cooldown'
    count = len(repo.all('pipeline_status'))
    again = restored.evaluate(NOW+timedelta(seconds=61), view=view(at=NOW+timedelta(seconds=60)))
    assert again['reference_event_id'] == last['reference_event_id']
    assert again['observed_at'] == last['observed_at'] and len(repo.all('pipeline_status')) == count


def test_unknown_does_not_clear_prior_risk_even_if_fail_open_is_selected(repo):
    monitor = active(repo, fail_closed=False)
    monitor.evaluate(NOW, view=view(risk=True))
    event = monitor.evaluate(NOW+timedelta(seconds=60), view={})
    assert event['status'] == 'UNKNOWN' and event['latched'] and event['block_entries']


@pytest.mark.parametrize('settings', [{'enabled': True, 'paper_shadow_only': False}, {'fail_closed': 'false'},
    {'clear_samples': 1}, {'cooldown_seconds': 0}, {'ief_15m_pct': '0.3'}, {'uso_30m_abs_pct': 'NaN'}])
def test_operating_activation_and_bad_config_are_rejected(settings):
    from sessionquant.session_config import SessionConfig
    with pytest.raises(ValueError): SessionConfig(macro_reference_filter=settings)


def test_reference_monitor_cannot_activate_on_real_data_engine(repo):
    with pytest.raises(ValueError): SessionEngine(repo, UniverseStore(), macro_reference_risk=active(repo))
    assert filter_settings({})['enabled'] is False


def run_tape(repo, rows, opened, *, monitor=None, provider=None):
    universe = UniverseStore(); universe.refresh(['AAPL'], source='SYNTHETIC_FIXTURE', observed_at=opened)
    engine = SessionEngine(repo, universe, synthetic=True, macro_reference_risk=monitor, risk_provider=provider)
    for at in sorted({event_time(row) for row in rows}):
        engine.apply_batch([row for row in rows if event_time(row) == at], at)
    return engine


def test_filter_off_preserves_exact_baseline_orders_fills_and_cash(repo, tmp_path):
    rows, opened = synthetic_tape()
    baseline_db = Repository(tmp_path/'baseline.db', backend='sqlite')
    try:
        baseline = run_tape(baseline_db, rows, opened)
        sample = run_tape(repo, rows, opened, monitor=MacroReferenceRiskMonitor(repo))
        assert sample.broker.to_state() == baseline.broker.to_state()
        assert sample.consumed_signals == baseline.consumed_signals
    finally: baseline_db.close()


def test_active_unknown_blocks_new_signals_and_cancels_pending_buy(repo):
    rows, opened = synthetic_tape()
    sample = run_tape(repo, rows, opened, monitor=active(repo))
    assert sample.broker.orders == {}
    assert any('macro_reference_risk_restriction' in row['blockers'] for row in repo.all('candidates'))
    at = NOW+timedelta(seconds=1)
    sample.broker.submit_order(intent(at=at))
    sample.tick(at)
    assert sample.broker.get_order('buy').status == 'cancelled'


@pytest.mark.parametrize('failure', ['429', 'timeout'])
def test_llm_fault_and_etf_risk_cannot_stop_own_stop_or_liquidation(repo, failure):
    class Broken:
        def risk_context(self, *args, **kwargs):
            if failure == 'timeout': raise httpx.ReadTimeout('PRIVATE')
            httpx.Response(429, request=httpx.Request('GET', 'https://fixture.invalid')).raise_for_status()
    universe = UniverseStore(); universe.refresh(['AAPL'], source='SYNTHETIC_FIXTURE', observed_at=NOW-timedelta(days=1))
    repo.set_control(VIEW, view(risk=True, down=True))
    sample = SessionEngine(repo, universe, synthetic=True, macro_reference_risk=active(repo), risk_provider=Broken())
    sample.broker.submit_order(intent(at=NOW))
    sample.broker.process_quote(quote(1, at=NOW))
    assert sample.broker.positions
    # ETF 위험만으로 보유 주식을 매도하지 않는다.
    q = quote(2, at=NOW)
    sample.apply_batch([{'kind': 'quote', 'data': jsonable(q)}], q.received_at)
    assert sample.broker.positions and not [o for o in sample.broker.orders.values() if o.intent.side == 'sell']
    for second in (3, 4):
        q = quote(second, '95', '95.1', at=NOW)
        sample.apply_batch([{'kind': 'quote', 'data': jsonable(q)}], q.received_at)
    assert not sample.broker.positions and not sample.failed
    assert any(o.intent.reason == 'stop_loss' for o in sample.broker.orders.values())
    assert repo.get_control(STATUS)['force_exit'] is False
    assert 'PRIVATE' not in str(repo.all('system_logs'))


def test_active_risk_does_not_override_regular_1440_exit_clock(repo):
    at = NOW.replace(hour=19, minute=40)
    repo.set_control(VIEW, view(risk=True, down=True, at=at))
    sample = SessionEngine(repo, UniverseStore(), synthetic=True, macro_reference_risk=active(repo))
    sample.broker.submit_order(intent(at=NOW)); sample.broker.process_quote(quote(1, at=NOW))
    sample.tick(at)
    assert sample.broker.exit_intents['AAPL']['reason'] == 'session_exit'
    assert not repo.get_control('sessionquant_context')['session']['liquidation_complete']
    for second in (1, 2):
        q = quote(second, at=at)
        sample.apply_batch([{'kind': 'quote', 'data': jsonable(q)}], q.received_at)
    assert not sample.broker.positions
    assert repo.get_control('sessionquant_context')['session']['liquidation_complete']


@pytest.mark.parametrize('symbol,minutes,value,trigger', [('IEF','15','-.30','IEF_15m_drop'),
    ('TLT','15','-.70','TLT_15m_drop'), ('USO','30','2','USO_30m_move'), ('USO','30','-2','USO_30m_move')])
def test_initial_hypotheses_are_inclusive_etf_percent_changes(repo, symbol, minutes, value, trigger):
    data = view(down=True)
    data['items'][symbol]['windows'][minutes]['pct_change'] = value
    event = active(repo).evaluate(NOW, view=data)
    assert event['status'] == 'ELEVATED' and event['triggers'] == [trigger]
    assert event['threshold_version'] == 'hypothesis_v1'
