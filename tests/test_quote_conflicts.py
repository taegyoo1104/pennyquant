"""Disputed book timestamps cannot drive entries, fills or risk exits."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.engine import MarketEngine
from sessionquant.market.universe import UniverseStore
from sessionquant.models import Quote
from sessionquant.session_demo import synthetic_tape
from sessionquant.session_engine import SessionEngine, event_time


@pytest.fixture
def session(tmp_path):
    rows, opened = synthetic_tape()
    entry = opened + timedelta(minutes=75, seconds=1)
    universe = UniverseStore()
    universe.refresh(['AAPL'], source='SYNTHETIC_FIXTURE', observed_at=opened-timedelta(days=1))
    repo = Repository(tmp_path/'quotes.db', backend='sqlite')
    engine = SessionEngine(repo, universe, synthetic=True, strategy='pullback_simple', use_market_filter=False,
                           record_inputs=True)
    fill = next(row for row in rows if row['kind']=='quote' and event_time(row)==entry+timedelta(seconds=1))
    yield SimpleNamespace(repo=repo, universe=universe, engine=engine, entry=entry, rows=rows, fill=fill)
    repo.close()


def start(value):
    value.engine.apply_batch([row for row in value.rows if event_time(row)<=value.entry], value.entry)
    assert len(value.engine.broker.get_open_orders()) == 1


def later_book(value, offset, *, source_offset=None, bid=None):
    row = deepcopy(value.fill)
    row['data']['received_at'] = (value.entry+timedelta(seconds=offset)).isoformat()
    row['data']['timestamp'] = (value.entry+timedelta(seconds=offset if source_offset is None else source_offset)).isoformat()
    if bid is not None:
        row['data'].update(bid=bid, ask=bid+.02)
    return row


def test_identical_book_preserves_receipt_and_newer_source_book_updates(session):
    market = MarketEngine(session.universe)
    original = Quote('AAPL', session.entry, session.entry, 101.35, 101.37, 1000, 1000)
    market.on_quote(original)
    market.on_quote(replace(original, received_at=session.entry+timedelta(seconds=1)))
    assert market.quotes['AAPL'] == original
    newer = replace(original, timestamp=session.entry+timedelta(seconds=2),
                    received_at=session.entry+timedelta(seconds=2), bid=100, ask=100.02)
    market.on_quote(newer)
    assert market.quotes['AAPL'] == newer
    old_state = market.to_state()
    old_state.pop('quote_conflicts')
    restored = MarketEngine(session.universe)
    restored.restore(old_state)
    assert restored.quotes == market.quotes and restored.quote_conflicts == {}


@pytest.mark.parametrize('change', [{'bid':101.34}, {'ask_size':1}, {'halted':True}])
def test_conflicting_book_is_unavailable_until_new_source_timestamp(session, change):
    market = MarketEngine(session.universe)
    original = Quote('AAPL', session.entry, session.entry, 101.35, 101.37, 1000, 1000)
    market.on_quote(original)
    market.on_quote(replace(original, received_at=session.entry+timedelta(seconds=1), **change))
    assert 'AAPL' not in market.quotes
    assert market.quote_conflicts['AAPL'] == original.timestamp
    assert 'quote_timestamp_conflict' in market.evaluate('AAPL', session.entry+timedelta(seconds=1)).blockers
    restored = MarketEngine(session.universe)
    restored.restore(market.to_state())
    restored.on_quote(original)
    assert 'AAPL' not in restored.quotes
    newer = replace(original, timestamp=session.entry+timedelta(seconds=2),
                    received_at=session.entry+timedelta(seconds=2))
    restored.on_quote(newer)
    assert restored.quotes['AAPL'] == newer


@pytest.mark.parametrize('restart', [False, True])
def test_same_batch_conflict_cannot_fill_pending_buy_and_survives_restart(session, restart):
    start(session)
    engine = session.engine
    engine.apply_batch([session.fill, later_book(session, 2, source_offset=1, bid=100)],
                       session.entry+timedelta(seconds=2))
    assert not engine.broker.fills and not engine.broker.positions
    metadata = session.repo.get_control('sq_market_meta_AAPL')
    assert metadata['quotes'] == {}
    assert metadata['quote_conflicts']['AAPL'] == session.fill['data']['timestamp']
    if restart:
        engine = SessionEngine(session.repo, session.universe, synthetic=True, strategy='pullback_simple',
                               use_market_filter=False, restore=True)
    engine.apply_batch([later_book(session, 3, source_offset=1)], session.entry+timedelta(seconds=3))
    assert not engine.broker.fills and 'AAPL' not in engine.market.quotes
    engine.apply_batch([later_book(session, 4)], session.entry+timedelta(seconds=4))
    assert [fill.side for fill in engine.broker.fills] == ['buy']


def test_late_conflict_in_batch_skips_disputed_book_and_preserves_newer_book(session):
    start(session)
    engine = session.engine
    newer = later_book(session, 2)
    engine.apply_batch([session.fill, newer, later_book(session, 3, source_offset=1, bid=100)],
                       session.entry+timedelta(seconds=3))
    assert engine.market.quote_conflicts['AAPL'] == session.entry+timedelta(seconds=1)
    assert engine.market.quotes['AAPL'].timestamp == session.entry+timedelta(seconds=2)
    assert len(engine.broker.fills) == 1
    assert engine.broker.fills[0].quote_timestamp == engine.market.quotes['AAPL'].timestamp
    restored = SessionEngine(session.repo, session.universe, synthetic=True, strategy='pullback_simple',
                             use_market_filter=False, restore=True)
    assert restored.market.to_state() == engine.market.to_state()


def test_disputed_price_does_not_trigger_stop_and_recording_replays_exactly(session, tmp_path, monkeypatch):
    from sessionquant.research import journal
    from sessionquant.research.bundle import export_recording
    from sessionquant.research.replay import replay_recording

    clock = SimpleNamespace(now=session.entry)
    monkeypatch.setattr(journal, 'clock', lambda: clock.now)
    journal.start(session.repo, session.engine, source='SYNTHETIC_FIXTURE')
    session.repo.record('universe_history', session.universe.snapshots[0])
    start(session)
    engine = session.engine
    clock.now = session.entry+timedelta(seconds=1)
    engine.apply_batch([session.fill], clock.now)
    mark = engine.broker.positions['AAPL'].mark_price
    clock.now = session.entry+timedelta(seconds=2)
    engine.apply_batch([later_book(session, 2, source_offset=1, bid=100)], clock.now)
    assert not engine.broker.exit_intents and not engine.broker.get_open_orders()
    assert engine.broker.positions['AAPL'].mark_price == mark
    clock.now = session.entry+timedelta(seconds=3)
    engine.apply_batch([later_book(session, 3, bid=100)], clock.now)
    assert engine.broker.get_open_orders()[0].intent.reason == 'stop_loss'
    clock.now = session.entry+timedelta(seconds=4)
    engine.apply_batch([later_book(session, 4, bid=100)], clock.now)
    assert [fill.side for fill in engine.broker.fills] == ['buy', 'sell']
    journal.stop(session.repo)
    export_recording(session.repo, tmp_path/'bundle')
    replay = replay_recording(tmp_path/'bundle', tmp_path/'replay.db', use_market_filter=False)
    assert replay['state'] == engine.broker.to_state()
