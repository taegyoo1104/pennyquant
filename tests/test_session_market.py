"""미래 자료 차단·거래 적격성·시장 필터와 네 장중 전략을 검증한다."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from sessionquant.market import InstrumentEligibility, MarketEngine, UniverseStore
from sessionquant.market.bars import aggregate_bars, indicators, session_bounds
from sessionquant.market.source import TossSessionSource
from sessionquant.market.strategies import technical_signal
from sessionquant.models import Bar, Quote


OPEN = datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)
NOW = OPEN + timedelta(minutes=75)
CASES = {
    "momentum": [100 + .1*i for i in range(14)] + [101.6],
    "pullback": [100 + .1*i for i in range(12)] + [101.05, 101., 101.35],
    "gap_breakout": [100, 100.05, 100.1] + [100.1]*11 + [100.5],
    "vwap": [101, 101, 101, 100, 100, 100, 100.1, 100.2, 100.3, 100.3, 100.4, 100.6, 100.5, 100.4, 100.8],
}


def minutes(symbol="AAPL", closes=None, opened=OPEN):
    closes = closes or CASES["pullback"]
    return [Bar(symbol, opened + timedelta(minutes=i*5+m), opened + timedelta(minutes=i*5+m),
                close, close+.15, close-.15, close, 150_000 if i == len(closes)-1 else 80_000)
            for i, close in enumerate(closes) for m in range(1, 6)]


def quote(symbol, value, now=NOW):
    return Quote(symbol, now, now, value-.01, value+.01, 1000, 1000, True, False, "NASDAQ")


def engine(strategy="pullback", *, use_market_filter=True):
    universe = UniverseStore()
    universe.refresh(["AAPL"], source="synthetic_fixture", observed_at=OPEN)
    result = MarketEngine(universe, strategy, use_market_filter=use_market_filter)
    for symbol in ("SPY", "QQQ", "AAPL"):
        closes = CASES[strategy] if symbol == "AAPL" else [100 + .25*i for i in range(15)]
        for bar in minutes(symbol, closes):
            result.on_bar(bar)
        result.on_quote(quote(symbol, closes[-1]))
    result.set_eligibility("AAPL", InstrumentEligibility("AAPL", NOW, "synthetic_fixture", True, True, False, False, "NASDAQ"))
    result.set_rvol("AAPL", 1.5, NOW, verified=True)
    return result


def test_universe_never_backdates_current_list_and_preserves_removal(tmp_path):
    path = tmp_path / "universe.json"
    store = UniverseStore(path)
    store.refresh(["AAPL", "MSFT"], source="fixture", observed_at=OPEN)
    assert not store.contains("AAPL", OPEN - timedelta(seconds=1))
    second = store.refresh(["AAPL", "NVDA"], source="fixture", observed_at=OPEN + timedelta(days=1))
    assert second["removed"] == ["MSFT"] and second["added"] == ["NVDA"]
    restored = UniverseStore(path)
    assert restored.contains("MSFT", OPEN) and not restored.contains("NVDA", OPEN)
    assert restored.contains("NVDA", OPEN + timedelta(days=1))
    assert not restored.members(OPEN + timedelta(days=9))
    assert len(json.loads(path.read_text())["snapshots"]) == 2


def test_universe_respects_separate_effective_and_known_times():
    store = UniverseStore()
    store.import_snapshots([{"effective_at": OPEN.isoformat(), "known_at": (OPEN + timedelta(days=2)).isoformat(),
                             "source": "delayed_historical_import", "members": ["AAPL"]}])
    assert not store.contains("AAPL", OPEN + timedelta(days=1))
    assert store.contains("AAPL", OPEN + timedelta(days=2))


async def test_public_refresh_allows_multiple_share_classes_and_rejects_partial_response(tmp_path):
    csv = "Symbol,Name,Sector\n" + "\n".join(f"T{i},Company{i},Technology" for i in range(503))
    store = UniverseStore(tmp_path / "sp500.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=csv))) as client:
        result = await store.refresh_public(client=client, observed_at=NOW)
    assert result["count"] == 503
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="Symbol,Name\nAAPL,Apple"))) as client:
        with pytest.raises(ValueError, match="종목 수"):
            await store.refresh_public(client=client, observed_at=NOW)
    assert len(store.members(NOW)) == 503


def test_aggregation_requires_all_minutes_and_available_receipts():
    bars = minutes()[:15]
    assert len(aggregate_bars(bars, 5, OPEN + timedelta(minutes=14, seconds=59))) == 2
    assert len(aggregate_bars(bars, 15, OPEN + timedelta(minutes=14, seconds=59))) == 0
    delayed = [replace(b, received_at=NOW) if i == 3 else b for i, b in enumerate(bars)]
    result = aggregate_bars(delayed, 5, OPEN + timedelta(minutes=15))
    assert len(result) == 2 and result[0].timestamp == OPEN + timedelta(minutes=10)
    assert aggregate_bars(delayed, 15, OPEN + timedelta(minutes=15)) == []
    assert aggregate_bars(bars[1:], 15, NOW) == []
    assert indicators(bars[1:], NOW).vwap is None


def test_conflicting_bar_revision_does_not_silently_choose_a_price():
    bars = minutes()[:5]
    assert len(aggregate_bars(bars + [bars[0]], 5, NOW)) == 1
    revision = replace(bars[0], close=bars[0].close + .01, received_at=NOW)
    assert aggregate_bars(bars + [revision], 5, NOW) == []
    assert len(aggregate_bars(bars + [revision], 5, NOW - timedelta(seconds=1))) == 1


def test_calendar_early_close_holiday_and_dst():
    early = datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)
    assert session_bounds(early)[1] == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    assert session_bounds(datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)) is None
    assert session_bounds(datetime(2026, 7, 2, 16, 0, tzinfo=timezone.utc))[0].hour == 13
    assert session_bounds(NOW)[0].hour == 14


@pytest.mark.parametrize("strategy", list(CASES))
def test_each_strategy_generates_without_positive_news_and_blocks_bearish_or_unknown(strategy):
    current = engine(strategy)
    decision = current.evaluate("AAPL", NOW)
    assert decision.eligible and decision.signal and decision.signal.strategy == strategy
    assert decision.signal.stop_price < decision.signal.reference_price
    assert decision.indicators["score_components"]["news"] == .5
    data = indicators(current.bars["AAPL"], NOW)
    for regime in ("BEARISH", "UNKNOWN", "NEUTRAL"):
        assert technical_signal("AAPL", strategy, data, regime, NOW) is None


def test_market_regimes_use_both_fresh_etfs_and_ignore_etf_order_permissions():
    current = engine()
    assert current.regime(NOW).state == "BULLISH"
    current.quotes["SPY"] = replace(current.quotes["SPY"], tradable=False)
    assert current.regime(NOW).state == "BULLISH"
    current.bars["QQQ"] = minutes("QQQ", [104-.25*i for i in range(15)])
    current.quotes["QQQ"] = quote("QQQ", 100.5)
    assert current.regime(NOW).state == "NEUTRAL"
    current.bars["SPY"] = minutes("SPY", [104-.25*i for i in range(15)])
    current.quotes["SPY"] = quote("SPY", 100.5)
    assert current.regime(NOW).state == "BEARISH"
    current.quotes["QQQ"] = replace(current.quotes["QQQ"], received_at=NOW + timedelta(seconds=1))
    assert current.regime(NOW).state == "UNKNOWN"


@pytest.mark.parametrize("field,value", [("fractional", None), ("tradable", None), ("halted", None),
                                        ("halted", True), ("corporate_action", None), ("corporate_action", True)])
def test_instrument_unknown_or_restrictions_do_not_become_tradable(field, value):
    current = engine()
    current.eligibility["AAPL"] = replace(current.eligibility["AAPL"], **{field: value})
    assert not current.evaluate("AAPL", NOW).eligible


def test_missing_rvol_and_news_block_and_stale_quotes_are_visible():
    current = engine()
    current.rvol.clear()
    assert "relative_volume_unknown_or_low" in current.evaluate("AAPL", NOW).blockers
    current.set_rvol("AAPL", 1.5, NOW, verified=False)
    assert not current.evaluate("AAPL", NOW).eligible
    current.set_rvol("AAPL", 1.5, NOW, verified=True)
    assert "confirmed_news_entry_restriction" in current.evaluate("AAPL", NOW, news_blocked=True).blockers
    assert "quote_stale" in current.evaluate("AAPL", NOW + timedelta(seconds=31)).blockers


def test_restore_preserves_future_boundaries_and_ablation_is_labelled():
    current = engine()
    state = current.to_state()
    restored = MarketEngine(current.universe)
    restored.restore(json.loads(json.dumps(state)))
    assert restored.evaluate("AAPL", NOW).to_dict() == current.evaluate("AAPL", NOW).to_dict()
    assert restored.evaluate("AAPL", NOW - timedelta(seconds=1)).signal is None
    ablation = engine(use_market_filter=False)
    del ablation.quotes["SPY"]
    decision = ablation.evaluate("AAPL", NOW)
    assert decision.regime == "UNKNOWN" and decision.signal
    assert decision.indicators["market_filter_enabled"] is False


async def test_source_prioritizes_held_symbols_isolates_errors_and_keeps_unknown_permissions():
    class FakeClient:
        async def stocks(self, symbols):
            return {"stocks": [{"symbol": symbol, "currency": "USD", "market": "NASDAQ", "status": "ACTIVE"}
                               for symbol in symbols]}

        async def candles(self, symbol, **kwargs):
            if symbol == "BROKEN":
                raise RuntimeError("secret should not be logged")
            return minutes(symbol), None

        async def quote(self, symbol, **kwargs):
            return replace(quote(symbol, 101.35), tradable=kwargs["tradable"])

    source = TossSessionSource(FakeClient(), candidates_per_cycle=1, clock=lambda: NOW)
    batch = await source.poll(["BROKEN", "AAPL"], NOW, priority_symbols=["MSFT"])
    assert [q.symbol for q in batch.quotes] == ["SPY", "QQQ", "MSFT", "BROKEN"]
    assert batch.eligibility[0].fractional is None and batch.eligibility[0].tradable is None
    assert all(not q.tradable for q in batch.quotes)
    assert "secret" not in str(batch.errors)
    second = await source.poll(["BROKEN", "AAPL"], NOW, priority_symbols=["MSFT"])
    assert [q.symbol for q in second.quotes] == ["SPY", "QQQ", "MSFT", "AAPL"]
    assert {bar.symbol for bar in second.bars} == {"AAPL"}
