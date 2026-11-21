from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from sessionquant.market.source import TossSessionSource
from sessionquant.models import Bar
from test_watchlist_source import Market
from watchlist_fixtures import NOW, context


@pytest.mark.parametrize('response_kind', ['stale', 'empty'])
async def test_delayed_closed_bar_retries_then_stops_without_renewing_old_bar(tmp_path, monkeypatch, response_kind):
    repo, _, _, _, manager = context(tmp_path)
    clock = SimpleNamespace(now=NOW+timedelta(minutes=5, seconds=1))
    monkeypatch.setattr(manager, 'light_symbols', lambda now: [])
    monkeypatch.setattr(manager, 'focus', lambda now: [])
    attempts = []

    class Client(Market):
        async def candles(self, symbol, **kwargs):
            attempts.append(clock.now)
            target = clock.now.replace(second=0)
            if len(attempts) == 1 and response_kind == 'empty':
                return [], None
            at = target-timedelta(minutes=1) if len(attempts) == 1 else target
            return [Bar(symbol, at, clock.now, 100, 101, 99, 100, 10)], None

    try:
        source = TossSessionSource(Client(lambda: clock.now), clock=lambda: clock.now, watchlist=manager,
            include_benchmarks=False, current_session_only=True, history_pages_per_cycle=0,
            quote_feed=SimpleNamespace(latest=lambda *a: object()))
        first = await source.poll([], priority_symbols=['T001'])
        assert manager.state['candle_refresh_v1']['T001']['status'] == 'AWAITING_PUBLICATION'
        if first.bars:
            assert first.bars[0].timestamp == NOW+timedelta(minutes=4)
        clock.now += timedelta(seconds=9)
        await source.poll([], priority_symbols=['T001'])
        assert len(attempts) == 1
        clock.now += timedelta(seconds=1)
        second = await source.poll([], priority_symbols=['T001'])
        assert len(attempts) == 2 and second.bars[-1].timestamp == NOW+timedelta(minutes=5)
        clock.now += timedelta(seconds=20)
        await source.poll([], priority_symbols=['T001'])
        assert len(attempts) == 2
        assert manager.state['candle_refresh_v1']['T001']['status'] == 'RECEIVED'
    finally:
        repo.close()


@pytest.mark.parametrize('throttle', [False, True])
async def test_missing_bar_retries_are_bounded_and_obey_provider_backoff(tmp_path, monkeypatch, throttle):
    repo, _, _, _, manager = context(tmp_path)
    clock = SimpleNamespace(now=NOW+timedelta(minutes=5, seconds=1))
    monkeypatch.setattr(manager, 'light_symbols', lambda now: [])
    monkeypatch.setattr(manager, 'focus', lambda now: [])
    attempts = []

    class Client(Market):
        async def candles(self, symbol, **kwargs):
            attempts.append(clock.now)
            if throttle and len(attempts) == 2:
                httpx.Response(429, headers={'Retry-After':'120'},
                    request=httpx.Request('GET', 'https://fake.test/candles')).raise_for_status()
            return [], None

    try:
        def source():
            return TossSessionSource(Client(lambda: clock.now), clock=lambda: clock.now, watchlist=manager,
                include_benchmarks=False, history_pages_per_cycle=0, max_api_calls=8,
                quote_feed=SimpleNamespace(latest=lambda *a: object()))
        current = source()
        for offset in (0, 10, 20, 30, 40, 50):
            clock.now = NOW+timedelta(minutes=5, seconds=1+offset)
            await current.poll([], priority_symbols=['T001'])
            assert manager.state['last_cycle_calls'] <= 8
            current = source()  # Persisted schedule must survive source recreation.
        assert len(attempts) == (2 if throttle else 3)
        clock.now = NOW+timedelta(minutes=6, seconds=1)
        await current.poll([], priority_symbols=['T001'])
        assert len(attempts) == (2 if throttle else 4)
    finally:
        repo.close()
