"""Ready inputs must reach decisions together, without widening trading gates."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sessionquant.market import InstrumentEligibility, MarketEngine
from sessionquant.market.bars import session_bounds
from sessionquant.market.source import TossSessionSource
from sessionquant.models import Bar, Quote
from test_session_market import minutes
from test_watchlist_source import Market
from watchlist_fixtures import NOW, context, observe


@pytest.mark.parametrize('stale_candidate', [False, True])
async def test_slow_persistence_delivers_bars_then_fresh_quotes_before_background(
        tmp_path, monkeypatch, stale_candidate):
    import sessionquant.market.source as source_module
    repo, universe, _, _, manager = context(tmp_path)
    elapsed = SimpleNamespace(seconds=60.)
    clock = lambda: NOW+timedelta(seconds=elapsed.seconds)
    monkeypatch.setattr(source_module, 'monotonic', lambda: elapsed.seconds)
    decisions = []
    market = MarketEngine(universe)
    candidate_closes = [100+.1*i for i in range(15)]+[101.35, 101.3, 101.65]

    class History:
        def ingest(self, bars):
            elapsed.seconds += 20  # Longer than the whole optional request window.
        def publish(self, *args, **kwargs): pass
        def ready(self, *args): return False

    class Client(Market):
        async def candles(self, symbol, **kwargs):
            self.calls.append(('candles', symbol))
            elapsed.seconds += .4
            closes = candidate_closes if symbol.startswith('T') else [100+.25*i for i in range(18)]
            return [replace(b, received_at=clock()) for b in minutes(symbol, closes)], None

        async def quote(self, symbol, **kwargs):
            self.calls.append(('quote', symbol))
            elapsed.seconds += .4
            event_at = clock()-timedelta(seconds=31 if stale_candidate and symbol.startswith('T4') else 0)
            value = candidate_closes[-1] if symbol.startswith('T') else 104.25
            return Quote(symbol, event_at, clock(), value-.01, value+.01, 1000, 1000, False, False, 'NASDAQ')

    def receive(batch):
        for bar in batch.bars: market.on_bar(bar)
        for row in batch.quotes: market.on_quote(row)
        candidates = [q.symbol for q in batch.quotes if q.symbol.startswith('T4')]
        if candidates:
            # Evidence is explicitly synthetic; this test isolates collection order.
            for symbol in candidates:
                market.set_eligibility(symbol, InstrumentEligibility(
                    symbol, clock(), 'SYNTHETIC_FIXTURE', True, True, False, False, 'NASDAQ'))
                market.set_rvol(symbol, 1.5, clock(), verified=True)
                decisions.append(market.evaluate(symbol, clock()))
            assert {'T400', 'T401'} <= {b.symbol for b in batch.bars}
            assert not any(s.startswith('T4') and s not in candidates
                           for kind, s in client.calls if kind == 'candles')
        elif batch.quotes:
            elapsed.seconds += 33  # Initial risk delivery/DB makes benchmark quotes stale.

    try:
        await manager.refresh(NOW)
        observe(manager)
        client = Client(clock)
        source = TossSessionSource(client, watchlist=manager, clock=clock,
            on_priority_batch=receive, history_store=History(), watchlist_candles_per_cycle=2)
        await source.poll([])
        assert len(decisions) == 2
        assert all(d.regime == 'BULLISH' for d in decisions)
        assert all(d.eligible and d.signal for d in decisions) if not stale_candidate else all(
            not d.eligible and 'quote_stale' in d.blockers for d in decisions)
        assert [s for kind, s in client.calls if kind == 'quote'].count('SPY') == 2
        assert [s for kind, s in client.calls if kind == 'quote'].count('QQQ') == 2
        assert client.calls.index(('candles', 'T401')) < client.calls.index(('quote', 'T400'))
        assert manager.state['last_cycle_calls'] <= source.max_api_calls
    finally:
        repo.close()


async def test_benchmark_paging_stops_at_today_open_without_consuming_stock_history_slot(tmp_path):
    repo, _, _, _, manager = context(tmp_path)
    clock = lambda: NOW+timedelta(seconds=60)
    opened, _ = session_bounds(clock())
    before = (opened-timedelta(days=35)).isoformat()

    class Client(Market):
        async def candles(self, symbol, **kwargs):
            self.calls.append(('backfill' if kwargs.get('before') else 'candles', symbol))
            return [], before

    try:
        await manager.refresh(NOW)
        observe(manager)
        manager.state['history_progress'] = {
            s: {'started': True, 'done': False, 'cursor': before, 'pages': 190,
                'status': 'PAGING'} for s in ('SPY', 'QQQ', 'T400')}
        client = Client(clock)
        source = TossSessionSource(client, watchlist=manager, clock=clock, history_pages_per_cycle=1)
        await source.poll([])
        assert [('backfill', 'T400')] == [r for r in client.calls if r[0] == 'backfill']
        for symbol in ('SPY', 'QQQ'):
            progress = manager.state['history_progress'][symbol]
            assert progress['done'] and progress['status'] == 'SESSION_WINDOW_REACHED'
            assert progress['pages'] == 190
    finally:
        repo.close()


async def test_benchmark_today_gap_rearms_paging_after_session_window_stop(tmp_path):
    repo, _, _, _, manager = context(tmp_path)
    stamp = NOW+timedelta(seconds=60)
    opened, _ = session_bounds(stamp)
    recent = stamp.replace(second=0)-timedelta(minutes=10)

    class Client(Market):
        async def candles(self, symbol, **kwargs):
            self.calls.append(('backfill' if kwargs.get('before') else 'candles', symbol))
            if kwargs.get('before'):
                return [Bar(symbol, opened+timedelta(minutes=2), stamp, 100, 101, 99, 100, 10)], opened.isoformat()
            return [Bar(symbol, recent, stamp, 100, 101, 99, 100, 10)], recent.isoformat()

    try:
        await manager.refresh(NOW)
        observe(manager)
        manager.state['history_progress'] = {
            s: {'started': True, 'done': True, 'cursor': (opened-timedelta(days=35)).isoformat(),
                'pages': 190, 'status': 'SESSION_WINDOW_REACHED'} for s in ('SPY', 'QQQ')}
        client = Client(lambda: stamp)
        source = TossSessionSource(client, watchlist=manager, clock=lambda: stamp, history_pages_per_cycle=1)
        source.restore_seen({s: [Bar(s, opened+timedelta(minutes=1), stamp, 100, 101, 99, 100, 10)]
                             for s in ('SPY', 'QQQ')})
        await source.poll([])
        assert any(kind == 'backfill' and s in {'SPY', 'QQQ'} for kind, s in client.calls)
        for symbol in ('SPY', 'QQQ'):
            progress = manager.state['history_progress'][symbol]
            assert progress['current_gap_at'] == (opened+timedelta(minutes=2)).isoformat()
    finally:
        repo.close()


async def test_background_candle_visit_does_not_postpone_candidate_quote_rotation(tmp_path):
    repo, _, _, _, manager = context(tmp_path)
    try:
        await manager.refresh(NOW)
        observe(manager)
        stamp = NOW+timedelta(seconds=60)
        manager.state['last_candidate_quote_attempt'] = {'T400': stamp.isoformat(), 'T401': stamp.isoformat()}
        # These candidates' bars were collected, but neither had a quote/decision.
        manager.scheduler.mark('T402', stamp)
        manager.scheduler.mark('T403', stamp)
        client = Market(lambda: stamp)
        source = TossSessionSource(client, watchlist=manager, clock=lambda: stamp)
        await source.poll([])
        assert [s for kind, s in client.calls if kind == 'quote' and s.startswith('T4')] == ['T402', 'T403']
    finally:
        repo.close()
