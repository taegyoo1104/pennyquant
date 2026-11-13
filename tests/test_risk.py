from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pennyquant.config.settings import Settings
from pennyquant.models import Account, Position, Quote, Signal
from pennyquant.trading.risk_manager import RiskManager, is_regular_session

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)


def quote(**kwargs):
    return replace(Quote("TEST", NOW, NOW, 1.998, 2.002, 100, 100), **kwargs)


def signal(**kwargs):
    return replace(Signal("signal-1", "TEST", "momentum", NOW, 2.0, "test"), **kwargs)


def test_whole_share_size_includes_costs_and_reserved_cash():
    manager = RiskManager(Settings())
    account = Account()
    decision = manager.evaluate(signal(), quote(), account, NOW)
    assert decision.approved and decision.qty == 6
    assert decision.limit_price == pytest.approx(2.01)
    risk = decision.qty * (decision.limit_price * (0.05 + 0.002 + 0.001) + 0.004)
    assert risk <= 0.7
    account.reserved_cash = 69
    assert not manager.evaluate(signal(), quote(), account, NOW).approved


@pytest.mark.parametrize("change,reason", [
    ({"received_at": NOW + timedelta(seconds=1)}, "future_or_inconsistent_timestamp"),
    ({"timestamp": NOW - timedelta(seconds=6)}, "stale_quote"),
    ({"exchange": "OTC"}, "instrument_not_tradable"),
    ({"halted": True}, "instrument_not_tradable"),
    ({"ask_size": 0}, "empty_quote_size"),
    ({"bid": 1.90}, "spread_too_wide"),
])
def test_quote_rejections(change, reason):
    decision = RiskManager(Settings()).evaluate(signal(), quote(**change), Account(), NOW)
    assert not decision.approved and decision.reason == reason


def test_daily_limits_include_unrealized_loss_and_stop_on_boundary():
    manager = RiskManager(Settings())
    assert manager.evaluate(signal(), quote(), Account(equity=68.6), NOW).reason == "daily_loss_limit"
    assert manager.evaluate(signal(), quote(), Account(entries_today=10), NOW).reason == "daily_entry_limit"
    assert manager.evaluate(signal(), quote(), Account(consecutive_losses=2), NOW).reason == "consecutive_loss_limit"


def test_no_averaging_and_no_second_position():
    position = Position("TEST", 1, 2.0, 0.002, 1.9, 2.2, NOW)
    account = Account(positions={"TEST": position})
    manager = RiskManager(Settings(max_positions=1))
    assert manager.evaluate(signal(), quote(), account, NOW).reason == "averaging_prohibited"
    assert manager.evaluate(signal(symbol="OTHER"), quote(symbol="OTHER"), account, NOW).reason == "position_limit"


def test_signal_and_account_validation_fail_closed():
    manager = RiskManager(Settings())
    assert manager.evaluate(signal(timestamp=NOW + timedelta(seconds=1)), quote(), Account(), NOW).reason == "future_or_inconsistent_timestamp"
    assert manager.evaluate(signal(timestamp=NOW - timedelta(seconds=6)), quote(), Account(), NOW).reason == "stale_signal"
    assert manager.evaluate(signal(), quote(), Account(cash=float("nan")), NOW).reason == "invalid_account"
    assert manager.evaluate(signal(), quote(), Account(), NOW, halted=True).reason == "entry_halted"


def test_market_calendar_holidays_dst_and_early_close():
    t = lambda value: datetime.fromisoformat(value)
    assert not is_regular_session(t("2026-07-03T15:00:00+00:00"))  # observed Independence Day
    assert not is_regular_session(t("2026-11-14T15:00:00+00:00"))  # Saturday
    assert is_regular_session(t("2026-10-30T13:30:00+00:00"))
    assert not is_regular_session(t("2026-11-02T13:30:00+00:00"))
    assert is_regular_session(t("2026-11-02T14:30:00+00:00"))
    assert is_regular_session(t("2026-11-27T17:59:59+00:00"))
    assert not is_regular_session(t("2026-11-27T18:00:00+00:00"))
