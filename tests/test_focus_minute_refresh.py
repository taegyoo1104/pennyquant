"""The websocket focus must not wait five collector turns for current bars."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from sessionquant.market.source import TossSessionSource
from sessionquant.session_config import load_session_config
from sessionquant.watchlist.selection import WatchlistManager
from test_watchlist_source import Market
from watchlist_fixtures import NOW, context, observe


async def test_websocket_profile_refreshes_all_ten_focus_bars_in_one_turn(tmp_path):
    repo, universe, config, provider, _ = context(tmp_path, size=50)
    config = replace(config, strategy='pullback_simple', focus_selection='turnover', focus_size=10)
    manager = WatchlistManager(repo, universe, config, provider=provider, clock=lambda: NOW, allow_synthetic=True)
    profile = load_session_config('config/sessionquant.strategy-lab-websocket.example.yaml')
    clock = lambda: NOW+timedelta(seconds=60)
    try:
        await manager.refresh(NOW)
        observe(manager)
        client = Market(clock)
        delivered = []
        source = TossSessionSource(client, clock=clock, watchlist=manager,
            candidates_per_cycle=profile.symbols_per_cycle, include_benchmarks=False,
            current_session_only=True, quote_feed=SimpleNamespace(latest=lambda *a: None),
            on_priority_batch=lambda batch: delivered.extend(batch.bars),
            history_pages_per_cycle=0, watchlist_candles_per_cycle=0)
        await source.poll([])
        focus = set(manager.focus(clock()))
        assert len(focus) == 10
        assert {b.symbol for b in delivered} == focus
        assert not [row for row in client.calls if row[0] == 'quote']
        assert manager.state['last_cycle_calls'] <= source.max_api_calls
        # Same minute: do not repeatedly fetch the same completed bar.
        client.calls.clear()
        await source.poll([])
        assert not [row for row in client.calls if row[0] == 'candles']
    finally:
        repo.close()


async def test_websocket_candle_attempts_stay_fair_when_deadline_crosses_minute(tmp_path, monkeypatch):
    import sessionquant.market.source as module
    repo, universe, config, provider, _ = context(tmp_path, size=50)
    config = replace(config, strategy='pullback_simple', focus_selection='turnover', focus_size=10)
    elapsed = [60.]
    clock = lambda: NOW+timedelta(seconds=elapsed[0])
    monkeypatch.setattr(module, 'monotonic', lambda: elapsed[0])
    manager = WatchlistManager(repo, universe, config, provider=provider, clock=clock, allow_synthetic=True)

    class Client(Market):
        async def candles(self, symbol, **kwargs):
            result = await super().candles(symbol, **kwargs)
            elapsed[0] += 5  # Only one call fits in each five-second window.
            return result

    try:
        await manager.refresh(NOW)
        observe(manager)
        client = Client(clock)
        source = TossSessionSource(client, clock=clock, watchlist=manager,
            candidates_per_cycle=10, include_benchmarks=False,
            quote_feed=SimpleNamespace(latest=lambda *a: None), max_cycle_seconds=5,
            history_pages_per_cycle=0, watchlist_candles_per_cycle=0)
        for _ in range(10):
            await source.poll([])
            # Simulate unrelated work between rounds. All symbols are due again,
            # so sorting by identical stream-visit times would starve later names.
            elapsed[0] += 60
        fetched = [symbol for kind, symbol in client.calls if kind == 'candles']
        assert len(fetched) == 10
        assert len(set(fetched)) == 10
    finally:
        repo.close()
