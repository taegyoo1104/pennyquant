"""A forming daily candle must never become tomorrow's comparison close."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market_reference.collector import MarketReferenceMonitor
from sessionquant.market_reference.contracts import observation
from sessionquant.market_reference.store import ReferenceStore


NOW = datetime.fromisoformat('2026-11-20T14:55:00+00:00')
DAY = '2026-11-19T00:00:00-05:00'


def daily(price, received, *, day=DAY, volume='100'):
    received = datetime.fromisoformat(received)
    return observation('SPY', 'daily', {'open':price, 'high':price, 'low':price,
        'close':price, 'volume':volume, 'currency':'USD'}, day, received, received)


@pytest.fixture
def store(tmp_path):
    repo = Repository(tmp_path / 'reference.db', backend='sqlite')
    yield ReferenceStore(repo)
    repo.close()


@pytest.mark.parametrize('final', ['715', '729'])
def test_forming_price_is_not_close_and_final_response_recovers_after_restart(store, final):
    forming = daily('729', '2026-11-19T16:00:00+00:00')
    store.ingest(forming)
    restored = ReferenceStore(store.repo)
    assert restored.publish(NOW)['items']['SPY']['previous_close'] is None
    finished = daily(final, NOW.isoformat())
    assert finished['id'] != forming['id']
    restored.ingest(finished)
    assert restored.publish(NOW)['items']['SPY']['previous_close'] == final
    assert not store.repo.all('orders')


def test_legacy_false_revision_recovers_without_rewriting_audit(store):
    forming = daily('729', '2026-11-19T16:00:00+00:00')
    finished = daily('715', NOW.isoformat())
    finished['quality_status'] = 'REVISION_PENDING'
    finished['quality_reasons'].append('revision_unresolved')
    store.state['rows']['SPY:daily'] = [forming, finished]
    store.state['revised_keys'] = ['SPY:daily:' + finished['event_at']]
    original = deepcopy(store.state['rows'])
    assert store.publish(NOW)['items']['SPY']['previous_close'] == '715'
    assert store.state['rows'] == original


def test_reported_spy_baseline_recovers_715_87_and_positive_change(store):
    from sessionquant.dashboard.home_benchmarks import benchmark_cards
    forming = daily('728.86', '2026-11-19T15:59:30.160630+00:00')
    final = daily('715.87', '2026-11-20T14:57:11.398184+00:00')
    final['quality_status'] = 'REVISION_PENDING'
    final['quality_reasons'].append('revision_unresolved')
    store.state['rows']['SPY:daily'] = [
        daily('730.2', '2026-11-19T15:59:30.160630+00:00', day='2026-11-18T00:00:00-05:00'),
        forming, final]
    store.state['revised_keys'] = ['SPY:daily:'+final['event_at']]
    now = datetime.fromisoformat('2026-11-20T15:03:53+00:00')
    store.ingest(observation('SPY','price',{'price':'716.16','currency':'USD'}, now, now, now))
    card = benchmark_cards(store.publish(now, symbols=['SPY']), now)[0]
    assert card['change'] == '+0.04%' and card['tone'] == 'up'
    assert str(card['reference']) == '715.87'


def test_conflicting_completed_prices_are_still_blocked(store):
    store.ingest(daily('715', NOW.isoformat()))
    store.ingest(daily('716', (NOW + timedelta(seconds=1)).isoformat()))
    assert store.publish(NOW + timedelta(seconds=1))['items']['SPY']['previous_close'] is None


def test_old_day_does_not_substitute_for_previous_close(store):
    store.ingest(daily('729', NOW.isoformat(), day='2026-11-18T00:00:00-05:00'))
    assert store.publish(NOW)['items']['SPY']['previous_close'] is None


@pytest.mark.parametrize('day,received,complete', [
    ('2026-10-30T00:00:00-04:00', '2026-10-30T19:59:59+00:00', False),
    ('2026-10-30T00:00:00-04:00', '2026-10-30T20:00:00+00:00', True),
    ('2026-11-27T00:00:00-05:00', '2026-11-27T17:59:59+00:00', False),
    ('2026-11-27T00:00:00-05:00', '2026-11-27T18:00:00+00:00', True),
])
def test_finality_uses_exchange_close_including_dst_and_short_days(day, received, complete):
    assert daily('100', received, day=day)['daily_complete'] is complete


async def test_missing_close_retries_even_if_endpoint_was_observed(store):
    now = [NOW]
    monitor = MarketReferenceMonitor(store.repo, clock=lambda:now[0], symbols=['SPY'],
                                     display_only=True, stream_prices=True)
    monitor.store.state['capabilities'] = {'SPY':{'daily':{'status':'OBSERVED'}}}
    monitor.store.state['schedule'] = {'daily:SPY':(NOW-timedelta(minutes=6)).isoformat()}
    calls = []
    async def read(stream, symbol):
        calls.append((stream, symbol))
        return {'data':{'candles':[{'timestamp':DAY, 'openPrice':'715', 'highPrice':'715',
            'lowPrice':'715', 'closePrice':'715', 'volume':'100', 'currency':'USD'}]}}
    view = await monitor.poll(read)
    assert calls == [('daily', 'SPY')]
    assert view['items']['SPY']['previous_close'] == '715'
    now[0] += timedelta(minutes=6)
    await monitor.poll(read)
    assert len(calls) == 1
