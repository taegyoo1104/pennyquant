from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pennyquant.config.settings import Settings
from pennyquant.models import Bar, NewsEvent, Quote, StockSnapshot
from pennyquant.strategies import STRATEGIES
from pennyquant.strategies.base import StrategyContext, session_vwap


UTC = timezone.utc


def make_context(name: str, *, start: datetime | None = None) -> StrategyContext:
    start = start or datetime(2026, 11, 13, 14, 31, tzinfo=UTC)
    patterns = {
        "momentum": [2.00, 2.02, 2.05, 2.08],
        "pullback": [2.00, 2.05, 2.15, 2.20, 2.16, 2.15, 2.18],
        "gap_breakout": [2.00, 2.01, 2.02, 2.015, 2.02, 2.04],
        "vwap": [2.00, 2.16, 2.20, 2.12, 2.10, 2.15],
    }
    closes = patterns[name]
    bars = []
    for index, close in enumerate(closes):
        opened = closes[index - 1] if index else close
        timestamp = start + timedelta(minutes=index)
        bars.append(Bar("PQEX", timestamp, timestamp + timedelta(milliseconds=100), opened,
                        max(opened, close) + 0.005, min(opened, close) - 0.005, close,
                        2500 if index == len(closes) - 1 else 1000))
    now = bars[-1].received_at
    quote = Quote("PQEX", now, now, closes[-1] - 0.002, closes[-1] + 0.002, 500, 500)
    snapshot = StockSnapshot(quote, 1.75, 1_000_000, 2_500_000, 4.0, max(bar.high for bar in bars))
    news_time = start - timedelta(minutes=2)
    news = NewsEvent("n1", ("PQEX",), "FDA approves new treatment", news_time, news_time + timedelta(seconds=1),
                     "testwire", real_time=True)
    return StrategyContext(bars, snapshot, [news], now, Settings())


@pytest.mark.parametrize("name", STRATEGIES)
def test_each_strategy_emits_on_its_closed_bar_pattern(name):
    context = make_context(name)
    signal = STRATEGIES[name]().evaluate(context)
    assert signal is not None
    assert signal.strategy == name
    assert signal.symbol == "PQEX"
    assert signal.timestamp == context.now
    assert signal.reference_price == context.bars[-1].close
    assert signal.news_id == "n1"


@pytest.mark.parametrize("name", STRATEGIES)
def test_no_strategy_buys_without_news_or_confirmed_relative_volume(name):
    context = make_context(name)
    assert STRATEGIES[name]().evaluate(replace(context, news=[])) is None
    unknown_rvol = replace(context.snapshot, relative_volume=None)
    assert STRATEGIES[name]().evaluate(replace(context, snapshot=unknown_rvol)) is None


@pytest.mark.parametrize("name", STRATEGIES)
def test_news_and_bars_are_invisible_until_received(name):
    context = make_context(name)
    future_news = replace(context.news[0], received_at=context.now + timedelta(seconds=1))
    assert STRATEGIES[name]().evaluate(replace(context, news=[future_news])) is None
    delayed_bar = replace(context.bars[-1], received_at=context.now + timedelta(seconds=1))
    assert STRATEGIES[name]().evaluate(replace(context, bars=context.bars[:-1] + [delayed_bar])) is None


@pytest.mark.parametrize("name", STRATEGIES)
def test_low_volume_confirmation_is_not_an_entry(name):
    context = make_context(name)
    small_latest = replace(context.bars[-1], volume=500)
    assert STRATEGIES[name]().evaluate(replace(context, bars=context.bars[:-1] + [small_latest])) is None


@pytest.mark.parametrize("name", STRATEGIES)
def test_repeated_evaluation_has_stable_signal_id_and_no_future_leakage(name):
    context = make_context(name)
    baseline = STRATEGIES[name]().evaluate(context)
    assert baseline is not None
    future = replace(context.bars[-1], timestamp=context.now + timedelta(minutes=1),
                     received_at=context.now + timedelta(minutes=1, seconds=1), close=4, high=4.1)
    with_future = replace(context, bars=context.bars + [future])
    assert STRATEGIES[name]().evaluate(with_future).id == baseline.id
    assert STRATEGIES[name]().evaluate(replace(context, now=context.now + timedelta(seconds=1))).id == baseline.id


@pytest.mark.parametrize("name", STRATEGIES)
def test_known_delayed_adverse_news_overrides_positive_catalyst(name):
    context = make_context(name)
    negative = NewsEvent("n2", ("PQEX",), "Company announces public offering of common stock",
                         context.now - timedelta(hours=1), context.now, "sec", real_time=False)
    assert STRATEGIES[name]().evaluate(replace(context, news=context.news + [negative])) is None


@pytest.mark.parametrize("name", STRATEGIES)
def test_pre_news_price_pattern_does_not_confirm_market_reaction(name):
    context = make_context(name)
    just_received = replace(context.news[0], published_at=context.now - timedelta(seconds=1), received_at=context.now)
    assert STRATEGIES[name]().evaluate(replace(context, news=[just_received])) is None


@pytest.mark.parametrize("name", STRATEGIES)
def test_old_or_missing_latest_bar_blocks_entry(name):
    context = make_context(name)
    new_now = context.now + timedelta(minutes=2)
    fresh_quote = replace(context.snapshot.quote, timestamp=new_now, received_at=new_now)
    assert STRATEGIES[name]().evaluate(replace(context, now=new_now, snapshot=replace(context.snapshot, quote=fresh_quote))) is None
    gapped = context.bars[:1] + context.bars[2:]
    assert STRATEGIES[name]().evaluate(replace(context, bars=gapped)) is None


def test_gap_first_five_minutes_follow_new_york_dst():
    summer = make_context("gap_breakout", start=datetime(2026, 7, 8, 13, 31, tzinfo=UTC))
    winter = make_context("gap_breakout", start=datetime(2026, 11, 13, 14, 31, tzinfo=UTC))
    assert STRATEGIES["gap_breakout"]().evaluate(summer) is not None
    assert STRATEGIES["gap_breakout"]().evaluate(winter) is not None
    late_start = make_context("gap_breakout", start=datetime(2026, 11, 13, 14, 32, tzinfo=UTC))
    assert STRATEGIES["gap_breakout"]().evaluate(late_start) is None


def test_other_symbols_and_prior_sessions_do_not_change_vwap():
    context = make_context("vwap")
    prior = replace(context.bars[0], timestamp=context.bars[0].timestamp - timedelta(days=1),
                    received_at=context.bars[0].received_at - timedelta(days=1), volume=1_000_000)
    unrelated = replace(context.bars[0], symbol="OTHER", volume=1_000_000)
    baseline = STRATEGIES["vwap"]().evaluate(context)
    altered = STRATEGIES["vwap"]().evaluate(replace(context, bars=[prior, unrelated] + context.bars))
    assert baseline is not None
    assert altered is not None and altered.id == baseline.id


def test_vwap_is_unavailable_with_zero_volume():
    context = make_context("momentum")
    assert session_vwap([replace(bar, volume=0) for bar in context.bars]) is None
