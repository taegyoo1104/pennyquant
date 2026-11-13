import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from sessionquant.collectors.common import CapabilityUnavailable, DataQualityError
from sessionquant.config.settings import Settings
from sessionquant.database.repository import Repository
from sessionquant.engine import EventEngine
from sessionquant.events import Event, write_events
from sessionquant.models import NewsEvent, Position, Quote, StockSnapshot
from sessionquant.news.analysis import NewsAnalysis, event_digest
from sessionquant.pipeline import EventTail, _candidate_readiness, run_system
from sessionquant.collectors.market_scanner import Candidate
from sessionquant.pipeline_config import PipelineConfig


NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)


def snapshot(symbol="TEST", received=NOW):
    quote = Quote(symbol, received, received, 2.0, 2.005, 100, 100, tradable=True, exchange="NASDAQ")
    return StockSnapshot(quote, 1.5, 2_000_000, 4_000_000, 5, 2.1)


class FakeMarket:
    def __init__(self):
        self.calls = []
        self.closed = False
    async def rankings(self, **kwargs):
        self.calls.append("rankings")
        return {"rankedAt": NOW.isoformat(), "rankings": [{"symbol": "TEST", "currency": "USD",
            "price": {"lastPrice": "2", "changeRate": "0.2"}, "tradingVolume": "2000000", "tradingAmount": "4000000"}]}
    async def prices(self, symbols):
        self.calls.append("prices")
        return [{"symbol": symbol, "currency": "USD", "lastPrice": "2", "timestamp": NOW.isoformat()} for symbol in symbols]
    async def candles(self, symbol, **kwargs):
        self.calls.append("candles")
        return [], None
    async def get_quote(self, symbol):
        self.calls.append("quote")
        return Quote(symbol, NOW, NOW, 2, 2.005, 100, 100, tradable=False, exchange="UNKNOWN")
    async def aclose(self):
        self.closed = True


class FakeNews:
    status = []
    def __init__(self):
        self.calls = []
        self.closed = False
    async def refresh(self, symbols, now):
        self.calls.append(list(symbols))
        return []
    async def aclose(self):
        self.closed = True


async def no_sleep(seconds):
    await asyncio.sleep(0)


async def test_toss_candidates_and_primary_news_refresh_run_without_fabricating_orders(tmp_path):
    repo = Repository(tmp_path / "system.db")
    market, news = FakeMarket(), FakeNews()
    settings = Settings(news_policy="primary")
    engine = EventEngine(settings, repository=repo)
    try:
        result = await run_system(PipelineConfig(), repo, engine=engine, market=market, news_service=news,
                                  max_cycles=2, clock=lambda: NOW, sleep=no_sleep)
        assert result["status"] == "stopped" and result["cycles"] == 2
        assert result["active_symbols"] == ["TEST"]
        assert news.calls == [["TEST"]]
        assert len(repo.all("candidates")) == 1 and repo.all("candidates")[0]["readiness"] == "WATCH"
        assert repo.all("orders") == [] and engine.snapshots == {}
        assert "verified_market_snapshot_required" in repo.all("candidates")[0]["blockers"]
        assert not market.closed and not news.closed
    finally:
        repo.close()


async def test_slow_llm_does_not_stop_market_quotes_and_task_is_cancelled_on_shutdown(tmp_path):
    gate, cancelled = asyncio.Event(), asyncio.Event()
    class SlowNews(FakeNews):
        async def refresh(self, symbols, now):
            try:
                await gate.wait()
            finally:
                cancelled.set()
            return []
    repo = Repository(tmp_path / "system.db")
    market = FakeMarket()
    try:
        result = await run_system(PipelineConfig(), repo, market=market, news_service=SlowNews(),
                                  max_cycles=3, clock=lambda: NOW, sleep=no_sleep)
        assert result["cycles"] == 3 and market.calls.count("quote") == 3
        assert cancelled.is_set()
    finally:
        repo.close()


async def test_source_errors_do_not_prevent_existing_holdings_quote_polling(tmp_path):
    class BrokenMarket(FakeMarket):
        async def rankings(self, **kwargs):
            raise ValueError("PRIVATE-provider-error")
        async def candles(self, symbol, **kwargs):
            raise ValueError("PRIVATE-provider-error")
    repo = Repository(tmp_path / "system.db")
    engine = EventEngine(Settings(), repository=repo)
    engine.manager.account.positions["HELD"] = Position("HELD", 1, 2, 0, 1.9, 2.2, NOW)
    # Keep this test focused on provider orchestration rather than fills.
    engine.process = lambda event: None
    market = BrokenMarket()
    try:
        result = await run_system(PipelineConfig(), repo, engine=engine, market=market, news_service=FakeNews(),
                                  max_cycles=1, clock=lambda: NOW)
        assert "quote" in market.calls and result["status"] == "stopped"
        assert "PRIVATE" not in json.dumps(repo.get_control("market_provider_status"))
        assert "market_rankings_error" in engine.manager.halt_reasons
    finally:
        repo.close()


def test_event_tail_preserves_partial_lines_and_rejects_changed_consumed_history(tmp_path):
    path = tmp_path / "events.jsonl"
    event = Event("snapshot", snapshot())
    line = json.dumps(event.to_dict()).encode() + b"\n"
    path.write_bytes(line + b'{"kind":')
    tail = EventTail(path)
    assert len(tail.read()) == 1 and tail.offset == len(line)
    assert tail.read() == []
    restored = EventTail(path, tail.state())
    path.write_bytes(line.replace(b'"TEST"', b'"BEST"') + b'{"kind":')
    with pytest.raises(DataQualityError, match="changed"):
        restored.read()


async def test_external_verified_feed_does_not_get_toss_observations_mixed_into_engine(tmp_path):
    path = tmp_path / "events.jsonl"
    write_events(path, [Event("snapshot", snapshot())])
    repo = Repository(tmp_path / "system.db")
    engine = EventEngine(Settings(news_policy="primary"), repository=repo)
    try:
        await run_system(PipelineConfig(input_event_path=path), repo, engine=engine, market=FakeMarket(),
                         news_service=FakeNews(), max_cycles=1, clock=lambda: NOW)
        assert engine.quotes["TEST"].tradable and engine.quotes["TEST"].exchange == "NASDAQ"
        assert len(repo.all("orders")) == 0
        cursor = repo.get_control("pipeline_feed_cursor")
        assert cursor["offset"] == path.stat().st_size
        assert engine.last_time == NOW
    finally:
        repo.close()


async def test_feed_batch_and_cursor_roll_back_together_on_process_failure(tmp_path):
    path = tmp_path / "events.jsonl"
    write_events(path, [Event("snapshot", snapshot()), Event("snapshot", snapshot("NEXT", NOW + timedelta(seconds=1)))])
    repo = Repository(tmp_path / "system.db")
    class BrokenEngine:
        settings = Settings()
        manager = None
        def process(self, event):
            repo.set_control("test_processed", event.data.quote.symbol)
            if event.data.quote.symbol == "NEXT":
                raise ValueError("PRIVATE-event-error")
    try:
        with pytest.raises(RuntimeError, match="sanitized"):
            await run_system(PipelineConfig(input_event_path=path, discover_with_toss=False), repo,
                engine=BrokenEngine(), news_service=FakeNews(), max_cycles=1, clock=lambda: NOW)
        assert repo.get_control("pipeline_feed_cursor") is None
        assert repo.get_control("test_processed") is None
        assert "PRIVATE" not in json.dumps(repo.get_control("pipeline_status"))
    finally:
        repo.close()


async def test_live_config_rejects_paper_engine_and_runtime_source_binding_cannot_change(tmp_path):
    repo = Repository(tmp_path / "system.db")
    engine = EventEngine(Settings(), repository=repo)
    try:
        with pytest.raises(CapabilityUnavailable, match="verified broker"):
            await run_system(PipelineConfig(mode="live"), repo, engine=engine, market=FakeMarket(), news_service=FakeNews(), max_cycles=1)
        await run_system(PipelineConfig(), repo, engine=engine, market=FakeMarket(), news_service=FakeNews(),
                         max_cycles=1, clock=lambda: NOW)
        with pytest.raises(ValueError, match="source changed"):
            await run_system(PipelineConfig(input_event_path=tmp_path / "different.jsonl"), repo,
                             engine=engine, market=FakeMarket(), news_service=FakeNews(), max_cycles=1)
    finally:
        repo.close()


def analysis(sentiment="NEGATIVE", *, symbols=("TEST",), published=None):
    event = NewsEvent("rss:risk", symbols, "Company announces a public offering",
                      published or NOW - timedelta(seconds=30), NOW - timedelta(seconds=20), "Official IR")
    return NewsAnalysis("TEST", event, sentiment=sentiment, entry_eligible=sentiment == "POSITIVE",
        primary_verified=True, dilution_risk=sentiment == "NEGATIVE", source_kind="rss",
        analyzed_at=NOW, input_digest=event_digest(event, "TEST"), reason="Extracted primary evidence")


async def test_completed_negative_is_registered_before_remaining_llm_work_finishes(tmp_path):
    gate = asyncio.Event()
    negative = analysis()
    class IncrementalNews(FakeNews):
        def __init__(self):
            super().__init__()
            self.analyses = {}
        async def refresh(self, symbols, now):
            self.analyses["risk:TEST"] = negative
            await gate.wait()
            return [negative]
    repo = Repository(tmp_path / "system.db")
    engine = EventEngine(Settings(news_policy="primary"), repository=repo)
    try:
        result = await run_system(PipelineConfig(), repo, engine=engine, market=FakeMarket(),
            news_service=IncrementalNews(), max_cycles=2, clock=lambda: NOW, sleep=no_sleep)
        assert result["analysis_count"] == 1
        assert len(engine.analyses) == len(repo.all("news_analysis")) == 1
        assert repo.all("candidates")[0]["readiness"] == "BLOCKED"
        assert "negative_news_or_dilution_risk" in repo.all("candidates")[0]["blockers"]
    finally:
        repo.close()


async def test_unassociated_unknown_rss_analysis_is_recorded_without_attaching_query_ticker(tmp_path):
    unknown = analysis("UNKNOWN", symbols=())
    class UnmappedNews(FakeNews):
        async def refresh(self, symbols, now):
            return [unknown]
    repo = Repository(tmp_path / "system.db")
    engine = EventEngine(Settings(news_policy="primary"), repository=repo)
    try:
        await run_system(PipelineConfig(), repo, engine=engine, market=FakeMarket(), news_service=UnmappedNews(),
                         max_cycles=1, clock=lambda: NOW)
        assert len(repo.all("news_analysis")) == 1 and engine.analyses == {}
        assert "news_analysis_registration_error" not in engine.manager.halt_reasons
        assert repo.all("candidates")[0]["readiness"] == "WATCH"
    finally:
        repo.close()


async def test_async_execution_drain_is_invoked_after_cycle_events(tmp_path):
    repo = Repository(tmp_path / "system.db")
    engine = EventEngine(Settings(), repository=repo)
    calls = []
    async def drain():
        calls.append(engine.last_time)
        return True
    engine.manager.drain = drain
    try:
        await run_system(PipelineConfig(), repo, engine=engine, market=FakeMarket(), news_service=FakeNews(),
                         max_cycles=2, clock=lambda: NOW, sleep=no_sleep)
        assert calls == [NOW, NOW]
    finally:
        repo.close()


def test_candidate_ready_uses_actual_source_policy_and_expiry_instead_of_cached_boolean():
    settings = Settings(news_policy="primary")
    engine = EventEngine(settings)
    engine.snapshots["TEST"] = snapshot()
    old = analysis("POSITIVE", published=NOW - timedelta(hours=6))
    engine.register_analysis(old)
    candidate = Candidate("TEST", 2, .2, 2_000_000, 4_000_000, NOW, NOW)
    _candidate_readiness(candidate, engine, {"TEST": old.to_dict()}, NOW, settings)
    assert candidate.readiness == "WATCH" and "primary_news_analysis_required" in candidate.blockers
