"""Early exits use actual net proceeds, closed bars and durable position intent."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from pennyquant.config.settings import Settings
from pennyquant.engine import EventEngine
from pennyquant.events import Event
from pennyquant.models import Account, Bar, NewsEvent, Order, OrderStatus, Position, Quote, StockSnapshot
from pennyquant.news.analysis import ExtractedEvent, NewsAnalysis, NewsAnalyzer, event_digest
from pennyquant.trading.order_manager import PaperOrderManager


T = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
SYMBOL = "FIX0"


def settings(**changes):
    return Settings(initial_cash=1000.0, regular_hours_only=False, fee_model="toss_us",
                    exit_price_policy="net", **changes)


def held_manager(**changes):
    config = settings(**changes)
    manager = PaperOrderManager(config, Account(cash=979.98, equity=1000.0, day_start_equity=1000.0))
    stop, target = manager.costs.exit_levels(2.0, 10, 0.02)
    manager.account.positions[SYMBOL] = Position(SYMBOL, 10, 2.0, 0.02, stop, target,
                                                T - timedelta(minutes=5), "momentum")
    return manager


def quote(at=T, *, bid=2.06, size=100):
    return Quote(SYMBOL, at, at, bid, bid + 0.004, size, size)


def reversal_bars():
    result = []
    for index, price in enumerate((2.10, 2.12, 2.14, 2.06)):
        timestamp = T - timedelta(minutes=3 - index)
        result.append(Bar(SYMBOL, timestamp, timestamp + timedelta(milliseconds=100),
                          price - 0.005 if index < 3 else 2.14,
                          price + 0.01 if index < 3 else 2.15,
                          price - 0.01, price, 100 if index < 3 else 200))
    return result


def analysis(*, sentiment="NEGATIVE", verified=True, requires_verification=False,
             published_at=None, received_at=None, analyzed_at=None, source_kind="rss"):
    event = NewsEvent(f"{source_kind}:risk", (SYMBOL,), "Issuer announces clinical trial failure",
                      published_at or T - timedelta(seconds=2), received_at or T - timedelta(seconds=1),
                      "Verified issuer IR", "https://www.sec.gov/Archives/edgar/data/1/risk.htm"
                      if source_kind == "sec" else "https://issuer.example/risk",
                      "The clinical trial failed to meet its primary endpoint.", verified=verified, real_time=False)
    return NewsAnalysis(SYMBOL, event, sentiment=sentiment,
                        events=(ExtractedEvent("CLINICAL_FAILURE", "CONFIRMED", "NEGATIVE", "NEGATIVE",
                                               "HIGH", False, "failed to meet its primary endpoint"),),
                        confidence=0.95, requires_verification=requires_verification,
                        primary_verified=verified and source_kind in {"sec", "rss"},
                        source_kind=source_kind, model="fixture", reason="Confirmed primary event",
                        analyzed_at=analyzed_at or T, input_digest=event_digest(event, SYMBOL))


def test_profit_exit_below_ten_percent_waits_for_later_quote_fill():
    manager = held_manager()
    current = quote()
    position = manager.account.positions[SYMBOL]
    assert current.bid < position.target_price
    assert manager.costs.proceeds(current.bid, position.qty) > position.avg_price * position.qty + position.entry_fee
    assert manager.request_exit(SYMBOL, "bearish_profit_exit", T, profitable_only=True)
    order = manager.check_exits(current, T)
    assert order is not None and order.reason == "bearish_profit_exit" and order.qty == 10
    assert manager.on_quote(current, T) == []
    fills = manager.on_quote(quote(T + timedelta(seconds=1)), T + timedelta(seconds=1))
    assert sum(fill.qty for fill in fills) == 10
    assert not manager.account.positions
    assert 0 < manager.closed_trades[0]["return_pct"] < 10


def test_gross_gain_that_does_not_cover_costs_is_not_early_profit():
    manager = held_manager()
    current = quote(bid=2.003)
    position = manager.account.positions[SYMBOL]
    assert current.bid > position.avg_price
    assert manager.costs.proceeds(current.bid, position.qty) < position.avg_price * position.qty + position.entry_fee
    assert manager.request_exit(SYMBOL, "bearish_profit_exit", T, profitable_only=True)
    assert manager.check_exits(current, T) is None
    assert SYMBOL in manager.exit_intents
    later = quote(T + timedelta(seconds=1))
    assert manager.check_exits(later, later.received_at).reason == "bearish_profit_exit"


def test_confirmed_adverse_event_can_make_risk_exit_before_fixed_stop():
    manager = held_manager()
    current = quote(bid=1.98)
    assert current.bid > manager.account.positions[SYMBOL].stop_price
    assert manager.request_exit(SYMBOL, "adverse_news_risk_exit", T)
    order = manager.check_exits(current, T)
    assert order.reason == "adverse_news_risk_exit"
    manager.on_quote(quote(T + timedelta(seconds=1), bid=1.98), T + timedelta(seconds=1))
    assert manager.closed_trades[0]["net_pnl"] < 0
    assert manager.closed_trades[0]["reason"] == "adverse_news_risk_exit"


def test_exit_intent_survives_restart_stale_quote_and_partial_execution():
    manager = held_manager()
    assert manager.request_exit(SYMBOL, "bearish_profit_exit", T, profitable_only=True)
    assert manager.check_exits(quote(T - timedelta(seconds=10)), T) is None
    restored = held_manager()
    restored.restore_state(json.loads(json.dumps(manager.export_state())))
    assert SYMBOL in restored.exit_intents
    current = quote(T + timedelta(seconds=1), size=4)
    order = restored.check_exits(current, current.received_at)
    assert restored.check_exits(current, current.received_at) is order
    later = quote(T + timedelta(seconds=2), size=4)
    assert sum(fill.qty for fill in restored.on_quote(later, later.received_at)) == 4
    assert restored.account.positions[SYMBOL].qty == 6
    again = held_manager()
    again.restore_state(json.loads(json.dumps(restored.export_state())))
    assert again.check_exits(quote(T + timedelta(seconds=3)), T + timedelta(seconds=3)).client_id == order.client_id
    fills = again.on_quote(quote(T + timedelta(seconds=4)), T + timedelta(seconds=4))
    assert sum(fill.qty for fill in fills) == 6
    assert not again.account.positions
    assert sum(fill.qty for fill in again.fills if fill.side == "sell") == 10


def test_unknown_existing_sell_prevents_duplicate_early_exit():
    manager = held_manager()
    unknown = Order("unknown-sell", SYMBOL, "sell", 10, 2.05, T - timedelta(seconds=1), OrderStatus.UNKNOWN)
    manager.orders[unknown.client_id] = unknown
    manager.request_exit(SYMBOL, "bearish_profit_exit", T, profitable_only=True)
    assert manager.check_exits(quote(), T) is unknown
    assert len(manager.orders) == 1


def test_unqualified_technical_signal_expires_instead_of_selling_on_late_recovery():
    manager = held_manager()
    manager.request_exit(SYMBOL, "bearish_profit_exit", T, profitable_only=True)
    assert manager.check_exits(quote(bid=2.003), T) is None
    at = T + timedelta(seconds=manager.settings.early_exit_max_signal_age_seconds + 1)
    assert manager.check_exits(quote(at), at) is None
    assert not manager.orders


def test_qualified_exit_keeps_protecting_remaining_position_after_profit_disappears():
    manager = held_manager()
    manager.request_exit(SYMBOL, "bearish_profit_exit", T, profitable_only=True)
    first = manager.check_exits(quote(), T)
    assert first is not None
    assert manager.exit_intents[SYMBOL]["qualified"] is True
    manager.cancel(first.client_id, T + timedelta(seconds=31))
    at = T + timedelta(seconds=manager.settings.early_exit_max_signal_age_seconds + 10)
    current = quote(at, bid=1.98)
    assert current.bid > manager.account.positions[SYMBOL].stop_price
    retry = manager.check_exits(current, at)
    assert retry is not None and retry.client_id != first.client_id and retry.qty == 10
    manager.on_quote(quote(at + timedelta(seconds=1), bid=1.98), at + timedelta(seconds=1))
    assert manager.closed_trades[0]["net_pnl"] < 0


def test_old_position_exit_intent_cannot_liquidate_a_new_holding():
    manager = held_manager()
    manager.request_exit(SYMBOL, "bearish_profit_exit", T, profitable_only=True)
    manager.account.positions[SYMBOL].opened_at = T + timedelta(seconds=1)
    at = T + timedelta(seconds=2)
    assert manager.check_exits(quote(at), at) is None
    assert not manager.orders


def test_bearish_reversal_requires_closed_consecutive_bars_and_volume():
    from pennyquant.trading.exit_signals import bearish_reversal
    bars = reversal_bars()
    config = settings()
    opened = T - timedelta(minutes=5)
    assert bearish_reversal(bars, SYMBOL, opened, T + timedelta(milliseconds=100), config)
    assert not bearish_reversal(bars, SYMBOL, opened, T - timedelta(milliseconds=1), config)
    assert not bearish_reversal(bars, SYMBOL, opened, T + timedelta(minutes=3), config)
    assert not bearish_reversal(bars[:-1] + [replace(bars[-1], volume=100)], SYMBOL, opened,
                                T + timedelta(milliseconds=100), config)
    gapped = [replace(bars[0], timestamp=bars[0].timestamp - timedelta(minutes=1),
                      received_at=bars[0].received_at - timedelta(minutes=1)), *bars[1:]]
    assert not bearish_reversal(gapped, SYMBOL, opened, T + timedelta(milliseconds=100), config)


def test_held_position_can_exit_when_entry_price_filter_rejects_it():
    manager = held_manager(min_price=3.0)
    engine = EventEngine(manager.settings, manager=manager)
    for bar in reversal_bars():
        current = quote(bar.timestamp, bid=bar.close)
        engine.process(Event("snapshot", StockSnapshot(current, 1.75, 1000000, 2500000.0, 4.0, 2.20)))
        engine.process(Event("bar", bar))
    sells = [order for order in manager.orders.values() if order.side == "sell"]
    assert len(sells) == 1 and sells[0].reason == "bearish_profit_exit"


@pytest.mark.parametrize("source_kind", ["rss", "sec", "alpaca"])
def test_verified_adverse_analysis_requests_exit_for_held_position(source_kind):
    manager = held_manager()
    engine = EventEngine(manager.settings, manager=manager)
    engine.register_analysis(analysis(source_kind=source_kind))
    assert SYMBOL in manager.exit_intents
    order = manager.check_exits(quote(), T)
    assert order.reason == "adverse_news_risk_exit"


@pytest.mark.parametrize("changes", [
    {"sentiment": "MIXED"}, {"sentiment": "UNKNOWN"}, {"verified": False},
    {"requires_verification": True}, {"published_at": T - timedelta(days=2)},
    {"analyzed_at": T + timedelta(days=1)},
])
def test_uncertain_unverified_stale_or_future_analysis_cannot_force_sell(changes):
    manager = held_manager()
    engine = EventEngine(manager.settings, manager=manager)
    engine.last_time = T
    engine.register_analysis(analysis(**changes))
    assert SYMBOL not in manager.exit_intents


def test_upgrade_restores_previous_settings_without_losing_positions():
    manager = held_manager()
    state = json.loads(json.dumps(manager.export_state()))
    for name in ("early_exit_enabled", "bearish_exit_volume_ratio", "early_exit_max_signal_age_seconds"):
        state["settings"].pop(name)
    state.pop("exit_intents")
    restored = held_manager()
    restored.restore_state(state)
    assert restored.account.positions[SYMBOL].qty == 10
    assert restored.account.cash == manager.account.cash
    assert restored.exit_intents == {}


def test_protective_exit_replaces_unfilled_higher_profit_target():
    manager = held_manager()
    target = manager.check_exits(quote(bid=2.30), T)
    assert target.reason == "take_profit"
    at = T + timedelta(seconds=1)
    manager.request_exit(SYMBOL, "adverse_news_risk_exit", at)
    protective = manager.check_exits(quote(at, bid=2.06), at)
    assert target.status == OrderStatus.CANCELLED
    assert protective.reason == "adverse_news_risk_exit" and protective.qty == 10
    assert protective.limit_price < target.limit_price


async def test_negated_offering_rule_only_result_never_forces_liquidation():
    event = replace(analysis().event, title="Issuer clarifies financing",
                    body="The company is not conducting a public offering.")
    analyzer = NewsAnalyzer(api_key="")
    try:
        result = await analyzer.analyze(event, SYMBOL, source_kind="rss", primary_verified=True, now=T)
    finally:
        await analyzer.aclose()
    assert result.model == "rules-v2" and analyzer.calls == 0
    assert result.requires_verification and not result.entry_eligible
    manager = held_manager()
    EventEngine(manager.settings, manager=manager).register_analysis(result)
    assert manager.exit_intents == {} and manager.orders == {}


async def test_neutral_extraction_of_negated_offering_cannot_force_sell():
    evidence = "The company is not conducting a public offering."
    event = replace(analysis().event, title="Issuer clarifies financing", body=evidence)
    facts = {"ticker": SYMBOL, "events": [{"type": "OTHER", "status": "CONFIRMED",
        "company_impact": "NEUTRAL", "shareholder_impact": "NEUTRAL", "importance": "LOW",
        "dilution_risk": False, "evidence": evidence}], "overall_sentiment": "NEUTRAL", "confidence": 0.95,
        "requires_verification": False, "missing_information": [], "reason": "The issuer denies a new offering."}
    payload = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(facts)}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        result = await NewsAnalyzer(api_key="fixture", client=client).analyze(
            event, SYMBOL, source_kind="rss", primary_verified=True, now=T)
    assert not result.entry_eligible
    manager = held_manager()
    EventEngine(manager.settings, manager=manager).register_analysis(result)
    assert manager.exit_intents == {} and manager.orders == {}


async def test_real_analyzer_confirms_risk_before_engine_requests_protective_exit():
    event = analysis().event
    facts = {"ticker": SYMBOL, "events": [{"type": "CLINICAL_FAILURE", "status": "CONFIRMED",
        "company_impact": "NEGATIVE", "shareholder_impact": "NEGATIVE", "importance": "HIGH",
        "dilution_risk": False, "evidence": "failed to meet its primary endpoint"}],
        "overall_sentiment": "NEGATIVE", "confidence": 0.95, "requires_verification": False,
        "missing_information": [], "reason": "The issuer explicitly confirms a failed clinical endpoint."}
    payload = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(facts)}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        analyzer = NewsAnalyzer(api_key="fixture", client=client)
        result = await analyzer.analyze(event, SYMBOL, source_kind="rss", primary_verified=True, now=T)
    assert analyzer.calls == 1 and result.model != "rules-v2" and result.events
    manager = held_manager()
    EventEngine(manager.settings, manager=manager).register_analysis(result, now=result.analyzed_at)
    assert manager.exit_intents[SYMBOL]["reason"] == "adverse_news_risk_exit"
