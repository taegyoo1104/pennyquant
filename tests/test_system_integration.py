"""Integration of candidate discovery, verified facts, strategies and execution.

All issuer stories, prices and broker responses in this file are test fixtures.
No service credentials or real orders are used.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from pennyquant.collectors.market_scanner import parse_rankings
from pennyquant.config.settings import Settings
from pennyquant.database.repository import Repository
from pennyquant.engine import EventEngine
from pennyquant.events import Event
from pennyquant.models import Bar, NewsEvent, Quote, Signal, StockSnapshot
from pennyquant.news.analysis import NewsAnalyzer, event_digest
from pennyquant.pipeline_config import PipelineConfig


UTC = timezone.utc
START = datetime(2026, 11, 13, 14, 31, tzinfo=UTC)
EVIDENCE = "executed a binding supply agreement valued at $12 million"


def issuer_event(symbol: str, *, source_kind="rss") -> NewsEvent:
    """Delayed primary disclosure is not mislabeled real-time news."""
    return NewsEvent(
        f"{source_kind}:fixture-{symbol}", (symbol,), "Issuer confirms a new supply agreement",
        START - timedelta(minutes=12), START - timedelta(minutes=3),
        "Fixture issuer IR", f"https://issuer.example/{symbol}/release",
        f"The issuer {EVIDENCE}. This is a completed announcement.",
        verified=True, real_time=False,
    )


def model_response(symbol: str) -> dict:
    facts = {
        "ticker": symbol,
        "events": [{"type": "MAJOR_CONTRACT", "status": "CONFIRMED", "company_impact": "POSITIVE",
                    "shareholder_impact": "POSITIVE", "importance": "HIGH", "dilution_risk": False,
                    "evidence": EVIDENCE}],
        "overall_sentiment": "POSITIVE", "confidence": 0.95, "requires_verification": False,
        "missing_information": [], "reason": "A binding agreement is explicitly confirmed.",
    }
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(facts)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 80}}


async def analyze_symbols(symbols, *, source_kind="rss"):
    def respond(request):
        payload = json.loads(request.content)
        supplied = json.loads(payload["messages"][1]["content"])
        return httpx.Response(200, json=model_response(supplied["ticker"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        analyzer = NewsAnalyzer(api_key="fixture-key", client=client)
        return [await analyzer.analyze(issuer_event(symbol, source_kind=source_kind), symbol,
                                      source_kind=source_kind, primary_verified=source_kind == "rss",
                                      now=START - timedelta(minutes=2)) for symbol in symbols]


def settings(**overrides):
    return Settings(initial_cash=10000.0, news_policy="primary", fee_model="toss_us",
                    exit_price_policy="net", **overrides)


def discover(symbols):
    payload = {"rankedAt": START.isoformat(), "rankings": [
        {"symbol": symbol, "currency": "USD", "price": {"lastPrice": "2.08", "changeRate": "0.18"},
         "tradingVolume": "1000000", "tradingAmount": "2500000"} for symbol in symbols
    ]}
    return parse_rankings(payload, "TOP_GAINERS", START, PipelineConfig())


def feed_momentum(engine: EventEngine, symbols):
    closes = [2.00, 2.02, 2.05, 2.08]
    for index, close in enumerate(closes):
        timestamp = START + timedelta(minutes=index)
        received = timestamp + timedelta(milliseconds=100)
        previous = closes[index - 1] if index else close
        for symbol in symbols:
            quote = Quote(symbol, timestamp, received, close - 0.002, close + 0.002, 10000, 10000)
            snapshot = StockSnapshot(quote, 1.75, 1000000, 2500000.0, 4.0, close + 0.005)
            engine.process(Event("snapshot", snapshot))
            engine.process(Event("bar", Bar(symbol, timestamp, received, previous,
                                             max(previous, close) + 0.005, min(previous, close) - 0.005,
                                             close, 2500 if index == len(closes) - 1 else 1000)))
    return START + timedelta(minutes=len(closes) - 1, seconds=1)


@pytest.mark.asyncio
async def test_six_market_candidates_yield_at_most_five_exposures_with_primary_analysis(tmp_path):
    symbols = [f"FIX{index}" for index in range(6)]
    candidates = discover(symbols)
    assert [item.symbol for item in candidates] == symbols
    repo = Repository(tmp_path / "system.db")
    try:
        engine = EventEngine(settings(), "momentum", repo)
        for candidate in candidates:
            repo.record("candidates", candidate.to_dict())
        for analysis in await analyze_symbols(symbols):
            assert analysis.entry_eligible and not analysis.event.real_time
            engine.register_analysis(analysis)
        next_quote = feed_momentum(engine, symbols)
        assert len(engine.manager.orders) == 5
        assert engine.signal_count == 6 and engine.rejected_signals == 1
        assert engine.manager.last_rejection == "position_limit"
        for symbol in symbols:
            engine.process(Event("quote", Quote(symbol, next_quote, next_quote, 2.078, 2.082, 10000, 10000)))
        assert set(engine.manager.account.positions) == set(symbols[:5])
        assert len(repo.all("news_analysis")) == 6
        assert len(repo.all("executions")) == 5
        restored = EventEngine(engine.settings, "momentum", repo, restore=True)
        assert len(restored.manager.account.positions) == 5 and len(restored.analyses) == 6
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_alpaca_positive_explanation_cannot_authorize_any_primary_policy_order(tmp_path):
    repo = Repository(tmp_path / "auxiliary.db")
    try:
        engine = EventEngine(settings(), "momentum", repo)
        analysis = (await analyze_symbols(["FIX0"], source_kind="alpaca"))[0]
        assert analysis.sentiment == "POSITIVE" and not analysis.entry_eligible
        engine.register_analysis(analysis)
        feed_momentum(engine, ["FIX0"])
        assert engine.signal_count == 0 and not engine.manager.orders and not engine.manager.account.positions
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_daily_loss_notifies_once_and_preserves_exit_across_restart(tmp_path):
    repo = Repository(tmp_path / "daily-stop.db")
    try:
        config = settings(risk_per_trade=0.04)
        engine = EventEngine(config, "momentum", repo)
        engine.register_analysis((await analyze_symbols(["FIX0"]))[0])
        next_quote = feed_momentum(engine, ["FIX0"])
        engine.process(Event("quote", Quote("FIX0", next_quote, next_quote, 2.078, 2.082, 10000, 10000)))
        assert engine.manager.account.positions
        crash_time = next_quote + timedelta(seconds=1)
        engine.process(Event("quote", Quote("FIX0", crash_time, crash_time, 1.90, 1.904, 10000, 10000)))
        assert "daily_loss" in engine.manager.halt_reasons
        assert len(repo.all("alerts")) == 1
        assert engine.manager.resume("daily_loss") is False
        assert any(order.side == "sell" for order in engine.manager.orders.values())

        restored = EventEngine(config, "momentum", repo, restore=True)
        assert "daily_loss" in restored.manager.halt_reasons
        exit_time = crash_time + timedelta(seconds=1)
        restored.process(Event("quote", Quote("FIX0", exit_time, exit_time, 1.90, 1.904, 10000, 10000)))
        assert not restored.manager.account.positions
        assert "daily_loss" in restored.manager.halt_reasons
        assert len(repo.all("alerts")) == 1
        assert repo.all("executions")[-1]["side"] == "sell"
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_completed_pattern_before_analysis_cannot_create_a_backdated_entry(tmp_path):
    repo = Repository(tmp_path / "analysis-timing.db")
    try:
        engine = EventEngine(settings(), "momentum", repo)
        analysis = (await analyze_symbols(["FIX0"]))[0]
        late = replace(analysis, analyzed_at=START + timedelta(minutes=4))
        engine.register_analysis(late)
        feed_momentum(engine, ["FIX0"])
        assert not engine.manager.orders
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_second_symbol_does_not_fill_while_existing_holding_valuation_is_stale(tmp_path):
    repo = Repository(tmp_path / "multisymbol-freshness.db")
    try:
        engine = EventEngine(settings(), "momentum", repo)
        engine.register_analysis((await analyze_symbols(["FIX0"]))[0])
        next_quote = feed_momentum(engine, ["FIX0"])
        engine.process(Event("quote", Quote("FIX0", next_quote, next_quote, 2.078, 2.082, 10000, 10000)))
        assert set(engine.manager.account.positions) == {"FIX0"}
        submit_at = next_quote + timedelta(seconds=4)
        quote = Quote("FIX1", submit_at, submit_at, 2.078, 2.082, 10000, 10000)
        pending = engine.manager.submit(Signal(id="second-intent", symbol="FIX1", strategy="momentum",
                                              timestamp=submit_at, reference_price=2.08,
                                              reason="Previously confirmed signal", news_id="rss:fixture-FIX1"),
                                        quote, submit_at)
        assert pending is not None
        observed_at = next_quote + timedelta(seconds=6)
        engine.process(Event("quote", Quote("FIX1", observed_at, observed_at, 2.078, 2.082, 10000, 10000)))
        assert set(engine.manager.account.positions) == {"FIX0"}
        assert "stale_market_data" in engine.manager.halt_reasons
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_new_negative_analysis_cancels_pending_entry_and_notifies_once(tmp_path):
    repo = Repository(tmp_path / "news-risk.db")
    try:
        engine = EventEngine(settings(regular_hours_only=False), "momentum", repo)
        quote = Quote("FIX0", START, START, 2.0, 2.005, 10000, 10000)
        signal = Signal(id="pending-before-news", symbol="FIX0", strategy="momentum", timestamp=START,
                        reference_price=2.0, reason="Previously approved evidence")
        order = engine.manager.submit(signal, quote, START)
        assert order is not None
        positive = (await analyze_symbols(["FIX0"]))[0]
        event = replace(positive.event, id="rss:risk-FIX0", title="Company announces public offering",
                        body="The company launches a registered direct offering.",
                        received_at=START)
        negative = replace(positive, event=event, sentiment="NEGATIVE", entry_eligible=False,
                           dilution_risk=True, analyzed_at=START + timedelta(seconds=1),
                           input_digest=event_digest(event, "FIX0"))
        engine.register_analysis(negative)
        engine.register_analysis(negative)
        assert order.status.value == "cancelled"
        assert len(repo.all("alerts")) == 1
        assert repo.all("alerts")[0]["code"] == "news_risk"
    finally:
        repo.close()


def test_manual_stop_applies_without_another_market_tick(tmp_path):
    repo = Repository(tmp_path / "idle-stop.db")
    try:
        engine = EventEngine(settings(regular_hours_only=False), repository=repo)
        quote = Quote("FIX0", START, START, 2.0, 2.005, 10000, 10000)
        signal = Signal(id="idle-order", symbol="FIX0", strategy="momentum", timestamp=START,
                        reference_price=2.0, reason="Previously approved evidence")
        order = engine.manager.submit(signal, quote, START)
        assert order is not None
        repo.set_control("manual_halt", True)
        engine.apply_runtime_controls(START + timedelta(seconds=1))
        assert "manual" in engine.manager.halt_reasons and order.status.value == "cancelled"
        engine.manager.halt("daily_loss")
        repo.set_control("manual_halt", False)
        engine.apply_runtime_controls(START + timedelta(seconds=2))
        assert "manual" not in engine.manager.halt_reasons and "daily_loss" in engine.manager.halt_reasons
    finally:
        repo.close()
