"""Receipt order must not lose delayed market inputs or invent fresh quotes."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sessionquant.events import parse_event
from sessionquant.market.source import MarketBatch
from sessionquant.models import jsonable
from sessionquant.portfolio.selection import VIEW
from sessionquant.session_engine import SessionEngine, event_time
from sessionquant.session_runtime import batch_events
from test_realtime_paper import observed, listing, NOW  # noqa: F401


def pending_inputs(observed, kind):
    """Quote is delivered first while an already received input is still queued."""
    first_at = NOW + timedelta(seconds=1)
    tail = next(row for row in observed.rows if row['kind'] == 'bar'
                and row['data']['symbol'] == 'AAPL'
                and row['data']['timestamp'] == NOW.isoformat())
    withheld = []
    if kind in {'bar', 'both'}:
        withheld.append(tail)
    if kind in {'listing', 'both'}:
        withheld.extend(row for row in observed.rows if row['kind'] == 'eligibility')
    initial = [row for row in observed.rows if event_time(row) <= first_at and row not in withheld]
    observed.engine.apply_batch(sorted(initial, key=event_time), first_at)
    assert observed.repo.all('orders') == []
    return MarketBatch(
        bars=[parse_event(tail).data] if kind in {'bar', 'both'} else [],
        eligibility=[listing()] if kind in {'listing', 'both'} else [])


@pytest.mark.parametrize('kind', ['bar', 'listing', 'both'])
def test_delayed_inputs_reach_actual_paper_strategy_without_waiting_for_next_quote(observed, kind):
    delayed = pending_inputs(observed, kind)
    now = NOW + timedelta(seconds=2)
    observed.engine.tick(now)
    assert observed.engine._batch_candidate_scope == []
    events = batch_events(delayed, delivered_at=now, last_time=observed.engine.last_time)
    assert events and all(event_time(row) == now for row in events)
    observed.engine.apply_batch(events, now)

    frame = observed.repo.get_control(VIEW)
    assert frame['approved_count'] == 1
    assert frame['rows'][0]['indicators']['data_quality']['strategy_rvol'] > 1
    assert observed.engine.market.eligibility['AAPL'].checked_at == listing().checked_at
    if delayed.bars:
        actual = next(bar for bar in observed.engine.market.bars['AAPL'] if bar.timestamp == NOW)
        assert actual.received_at == delayed.bars[0].received_at
    # A bar/listing update can submit an order but cannot fill it using the old quote.
    assert not observed.engine.broker.fills
    next_at = now + timedelta(seconds=1)
    quote = replace(observed.engine.market.quotes['AAPL'], timestamp=next_at, received_at=next_at)
    observed.engine.apply_batch([{'kind': 'quote', 'data': jsonable(quote)}], next_at)
    assert [fill.side for fill in observed.engine.broker.fills] == ['buy']
    again = next_at + timedelta(seconds=1)
    observed.engine.apply_batch(batch_events(delayed, delivered_at=again,
                                last_time=observed.engine.last_time), again)
    assert len(observed.repo.all('order_intents')) == 1
    assert len(observed.engine.broker.fills) == 1


@pytest.mark.parametrize('seconds', [12, 32])
def test_delayed_bar_with_stale_quote_skips_expensive_candidate_pass(observed, seconds, monkeypatch):
    delayed = pending_inputs(observed, 'bar')
    quote = observed.engine.market.quotes['AAPL']
    frame = observed.repo.get_control(VIEW)
    calls = []
    evaluate = observed.engine.market.evaluate
    def counted(*args, **kwargs):
        calls.append(args)
        return evaluate(*args, **kwargs)
    monkeypatch.setattr(observed.engine.market, 'evaluate', counted)
    now = NOW + timedelta(seconds=seconds)
    observed.engine.apply_batch(batch_events(delayed, delivered_at=now,
                                last_time=observed.engine.last_time), now)
    assert calls == []
    assert observed.engine._batch_candidate_scope == []
    assert observed.repo.get_control(VIEW) == frame
    assert any(bar.timestamp == NOW for bar in observed.engine.market.bars['AAPL'])
    assert observed.engine.market.quotes['AAPL'] == quote
    assert observed.repo.all('orders') == []


@pytest.mark.parametrize('seconds', [12, 32])
def test_new_receipt_of_stale_market_quote_is_explicitly_rejected(observed, seconds):
    delayed = pending_inputs(observed, 'bar')
    now = NOW + timedelta(seconds=seconds)
    old = observed.engine.market.quotes['AAPL']
    delayed.quotes = [replace(old, received_at=now)]
    observed.engine.apply_batch(batch_events(delayed, delivered_at=now,
                                last_time=observed.engine.last_time), now)
    assert 'candidate_quote_stale_or_unknown' in observed.repo.get_control(VIEW)['rows'][0]['blockers']
    assert observed.engine.market.quotes['AAPL'].timestamp == old.timestamp
    assert observed.repo.all('orders') == []


def test_runtime_retains_cached_listing_restriction_but_drops_old_quote(observed):
    delayed = pending_inputs(observed, 'listing')
    delayed.eligibility = [replace(listing(), halted=True)]
    old = replace(observed.engine.market.quotes['AAPL'], timestamp=NOW, received_at=NOW)
    delayed.quotes = [old]
    now = NOW + timedelta(seconds=2)
    events = batch_events(delayed, delivered_at=now, last_time=observed.engine.last_time)
    assert [row['kind'] for row in events] == ['eligibility']
    observed.engine.apply_batch(events, now)
    assert 'paper_known_restriction_halted' in observed.repo.get_control(VIEW)['rows'][0]['blockers']
    assert observed.repo.all('orders') == []


def test_late_delivery_is_auditable_and_replays_to_identical_paper_state(observed, tmp_path, monkeypatch):
    from sessionquant.research import journal
    from sessionquant.research.bundle import export_recording
    from sessionquant.research.replay import replay_recording

    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(journal, 'clock', lambda: clock.now)
    journal.start(observed.repo, observed.engine, source='SYNTHETIC_DELAYED_DELIVERY')
    observed.repo.record('universe_history', observed.universe.snapshots[0])
    clock.now += timedelta(seconds=1)
    delayed = pending_inputs(observed, 'both')
    clock.now += timedelta(seconds=1)
    observed.engine.apply_batch(batch_events(delayed, delivered_at=clock.now,
                                last_time=observed.engine.last_time), clock.now)
    clock.now += timedelta(seconds=1)
    quote = replace(observed.engine.market.quotes['AAPL'], timestamp=clock.now, received_at=clock.now)
    observed.engine.apply_batch([{'kind': 'quote', 'data': jsonable(quote)}], clock.now)
    assert len(observed.engine.broker.fills) == 1
    frames = [row for row in observed.repo.all('research_events') if row['kind'] == 'engine_batch']
    recorded = [raw for frame in frames for raw in frame['data']['events'] if 'delivered_at' in raw]
    assert {row['kind'] for row in recorded} == {'bar', 'eligibility'}
    for raw in recorded:
        original = raw['data']['checked_at'] if raw['kind'] == 'eligibility' else raw['data']['received_at']
        assert raw['timing']['received_at'] == original
        assert raw['timing']['available_at'] == raw['delivered_at']
    journal.stop(observed.repo)
    export_recording(observed.repo, tmp_path/'delayed-recording')
    replay = replay_recording(tmp_path/'delayed-recording', tmp_path/'delayed-replay.db')
    assert replay['state'] == observed.engine.broker.to_state()
    restored = SessionEngine(observed.repo, observed.universe, synthetic=True, strict_market_data=True,
        paper_data_policy='observed_paper_v1', candidate_settings={'enabled': True}, restore=True)
    assert restored.broker.to_state() == observed.engine.broker.to_state()


def test_delivery_marker_rejects_future_and_retimed_quotes(observed):
    raw = next(row for row in observed.rows if row['kind'] == 'bar')
    with pytest.raises(ValueError):
        event_time({**raw, 'delivered_at': (event_time(raw)-timedelta(seconds=1)).isoformat()})
    quote = next(row for row in observed.rows if row['kind'] == 'quote')
    with pytest.raises(ValueError):
        event_time({**quote, 'delivered_at': (NOW+timedelta(seconds=2)).isoformat()})
    with pytest.raises(ValueError):
        observed.engine.apply_batch([{**raw, 'delivered_at': (NOW+timedelta(seconds=1)).isoformat()}], NOW)
