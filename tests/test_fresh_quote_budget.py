"""A candle timeout must not consume the whole candidate quote opportunity."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sessionquant.market.engine import MarketEngine
from sessionquant.market.source import TossSessionSource
from sessionquant.models import Bar, Quote
from test_watchlist_source import Market
from watchlist_fixtures import NOW, context, observe


@pytest.mark.parametrize('cycle_seconds', [5, 15])
async def test_slow_candles_leave_time_for_candidate_quotes(tmp_path, monkeypatch, cycle_seconds):
    import sessionquant.market.source as module
    repo, _, _, _, manager = context(tmp_path)
    elapsed = SimpleNamespace(seconds=60., timeout=None)
    clock = lambda: NOW + timedelta(seconds=elapsed.seconds)
    monkeypatch.setattr(module, 'monotonic', lambda: elapsed.seconds)

    async def wait_for(awaitable, *, timeout):
        elapsed.timeout = timeout
        try:
            return await awaitable
        finally:
            elapsed.timeout = None
    monkeypatch.setattr(asyncio, 'wait_for', wait_for)

    class Client(Market):
        async def prices(self, symbols):
            elapsed.seconds += min(2.5, elapsed.timeout)
            if elapsed.timeout < 2.5:
                raise TimeoutError
            return await super().prices(symbols)
        async def rankings(self, **kwargs):
            elapsed.seconds += min(2.5, elapsed.timeout)
            if elapsed.timeout < 2.5:
                raise TimeoutError
            return await super().rankings(**kwargs)
        async def candles(self, symbol, **kwargs):
            if symbol.startswith('T'):
                self.calls.append(('candles', symbol))
                # Model a genuinely timed-out request and its elapsed wall time.
                elapsed.seconds += elapsed.timeout
                raise TimeoutError
            return await super().candles(symbol, **kwargs)
        async def quote(self, symbol, **kwargs):
            elapsed.seconds += .1
            return await super().quote(symbol, **kwargs)

    deliveries = []
    try:
        await manager.refresh(NOW); observe(manager)
        client = Client(clock)
        source = TossSessionSource(client, watchlist=manager, clock=clock,
            max_cycle_seconds=cycle_seconds, on_priority_batch=lambda b: deliveries.extend(b.quotes))
        await source.poll([])
        assert {'T400', 'T401'} <= {q.symbol for q in deliveries}
        assert not source._seen.get('T400')
        assert manager.state['last_cycle_calls'] <= source.max_api_calls
        assert manager.state['focus_phase_seconds'] <= cycle_seconds
        assert any(r['source']=='toss_candles' and r['status'] in {'TimeoutError', 'quote_time_reserved'}
                   for r in manager.state['read_measurements'])
    finally:
        repo.close()


def test_regime_shared_only_inside_same_evaluation_and_invalidated_by_inputs(tmp_path, monkeypatch):
    import sessionquant.market.engine as module
    repo, universe, _, _, _ = context(tmp_path)
    engine = MarketEngine(universe)
    calls = []
    original = module.classify_regime
    def counted(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'classify_regime', counted)
    quote = Quote('SPY', NOW, NOW, 100, 100.01, 1000, 1000, False, False, 'NASDAQ')
    try:
        with engine.evaluation_scope():
            for _ in range(20): engine.regime(NOW)
            assert len(calls) == 1
            engine.on_quote(quote)
            engine.regime(NOW)
            assert len(calls) == 2
            engine.on_bar(Bar('SPY', NOW, NOW, 100, 101, 99, 100, 100))
            engine.regime(NOW)
            assert len(calls) == 3
            engine.on_quote(replace(quote, symbol='AAPL'))
            engine.regime(NOW)
            assert len(calls) == 3
            engine.regime(NOW+timedelta(seconds=31))
            assert len(calls) == 4
            engine.max_quote_age_seconds = 1
            engine.regime(NOW+timedelta(seconds=31))
            assert len(calls) == 5
        engine.regime(NOW+timedelta(seconds=31))
        assert len(calls) == 6
        with pytest.raises(RuntimeError), engine.evaluation_scope():
            engine.regime(NOW)
            raise RuntimeError('synthetic failure')
        assert engine._regime_cache is None
    finally:
        repo.close()


def test_shared_regime_never_extends_quote_freshness():
    from test_session_market import engine, NOW as at
    market = engine()
    with market.evaluation_scope():
        assert market.regime(at).state == 'BULLISH'
        later = at + timedelta(seconds=31)
        assert market.regime(later).state == 'UNKNOWN'
        for symbol in ('SPY', 'QQQ'):
            market.on_quote(replace(market.quotes[symbol], timestamp=later, received_at=later))
        assert market.regime(later).state == 'BULLISH'
    assert market._regime_cache is None
