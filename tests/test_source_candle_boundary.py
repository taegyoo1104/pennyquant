"""A slow candle response must not consume the following minute's refresh."""
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from sessionquant.market.source import TossSessionSource
from sessionquant.models import Bar
from test_watchlist_source import Market
from watchlist_fixtures import NOW, context


@pytest.mark.parametrize('slow_stage', ['http', 'persistence'])
async def test_candle_request_crossing_boundary_refreshes_next_minute(
        tmp_path, monkeypatch, slow_stage):
    import sessionquant.market.source as source_module
    repo, _, _, _, manager = context(tmp_path)
    elapsed = SimpleNamespace(seconds=359.)  # 16:05:59, two seconds before receipt.
    clock = lambda: NOW+timedelta(seconds=elapsed.seconds)
    monkeypatch.setattr(source_module, 'monotonic', lambda: elapsed.seconds)
    monkeypatch.setattr(manager, 'light_symbols', lambda now: [])
    monkeypatch.setattr(manager, 'focus', lambda now: [])
    requests = []

    class Client(Market):
        async def candles(self, symbol, **kwargs):
            self.calls.append(('candles', symbol))
            stamp = clock()
            requests.append(stamp)
            if slow_stage == 'http' and len(requests) == 1:
                elapsed.seconds += 2
            return [Bar(symbol, stamp.replace(second=0), clock(), 100, 101, 99, 100, 10)], None

    class History:
        def ingest(self, bars):
            if slow_stage == 'persistence' and len(requests) == 1:
                elapsed.seconds += 2
        def publish(self, *args, **kwargs): pass

    try:
        client = Client(clock)
        source = TossSessionSource(client, clock=clock, watchlist=manager,
            history_store=History(), include_benchmarks=False, history_pages_per_cycle=0)
        first = await source.poll([], priority_symbols=['T001'])
        assert len(requests) == 1
        assert first.bars[0].timestamp == NOW+timedelta(minutes=5)
        assert clock() == NOW+timedelta(minutes=6, seconds=1)

        second = await source.poll([], priority_symbols=['T001'])
        assert len(requests) == 2
        assert second.bars[0].timestamp == NOW+timedelta(minutes=6)
        assert manager.state['last_cycle_calls'] == 2  # Quote and fresh candles.

        third = await source.poll([], priority_symbols=['T001'])
        assert len(requests) == 2  # The successful minute-six request still deduplicates.
        assert third.bars == []
        assert manager.state['last_cycle_calls'] == 1  # Quote only.
    finally:
        repo.close()


@pytest.mark.parametrize('http_status', [503, 429])
async def test_failed_candle_request_does_not_mark_minute_or_bypass_backoff(
        tmp_path, monkeypatch, http_status):
    repo, _, _, _, manager = context(tmp_path)
    clock = SimpleNamespace(now=NOW+timedelta(minutes=5, seconds=59))
    monkeypatch.setattr(manager, 'light_symbols', lambda now: [])
    monkeypatch.setattr(manager, 'focus', lambda now: [])
    attempts = []

    class Client(Market):
        async def candles(self, symbol, **kwargs):
            attempts.append(self.clock())
            if len(attempts) == 1:
                self.calls.append(('candles', symbol))
                clock.now += timedelta(seconds=2)
                httpx.Response(http_status, headers={'Retry-After': '120'},
                    request=httpx.Request('GET', 'https://fake.test/candles')).raise_for_status()
            return await super().candles(symbol, **kwargs)

    try:
        client = Client(lambda: clock.now)
        source = TossSessionSource(client, clock=lambda: clock.now, watchlist=manager,
            include_benchmarks=False, history_pages_per_cycle=0, max_api_calls=8)
        failed = await source.poll([], priority_symbols=['T001'])
        assert failed.errors[0]['http_status'] == http_status
        assert 'T001' not in manager.state['last_bars_fetch']
        assert manager.state['last_cycle_calls'] == 3  # Metadata, quote, failed candles.

        await source.poll([], priority_symbols=['T001'])
        if http_status == 429:
            assert len(attempts) == 1
            assert manager.state['last_cycle_calls'] == 0
            clock.now += timedelta(seconds=120)
            await source.poll([], priority_symbols=['T001'])
        assert len(attempts) == 2
        assert manager.state['last_cycle_calls'] == 2
        assert manager.state['last_bars_fetch']['T001'] == clock.now.isoformat()
        assert not manager.state['budget_exhausted']
    finally:
        repo.close()
