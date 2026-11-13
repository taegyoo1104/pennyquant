from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pennyquant.models import Bar
from pennyquant.scanners.relative_volume import relative_volume


UTC = timezone.utc
NOW = datetime(2026, 11, 13, 14, 33, tzinfo=UTC)


def session_bars(day: str, volume: int, *, hour: int = 14, minutes: int = 3) -> list[Bar]:
    opened = datetime.fromisoformat(f"{day}T{hour:02}:30:00+00:00")
    return [Bar("PQEX", opened + timedelta(minutes=minute), opened + timedelta(minutes=minute, seconds=1),
                2.0, 2.1, 1.9, 2.0, volume) for minute in range(1, minutes + 1)]


def history():
    return session_bars("2026-11-12", 300) + session_bars("2026-11-11", 200) + session_bars("2026-11-10", 100)


def calculate(bars=None, current=900, now=NOW, **kwargs):
    return relative_volume(current, now, history() if bars is None else bars, symbol="PQEX",
                           min_sessions=3, corporate_actions_verified=True, **kwargs)


def test_same_elapsed_minute_cumulative_average():
    # Prior cumulative volumes: 900, 600, 300 -> mean600. Current900 ->1.5x.
    assert calculate() == pytest.approx(1.5)
    assert calculate(current=0) == 0.0


def test_unknown_corporate_action_quality_is_not_assumed():
    assert relative_volume(900, NOW, history(), symbol="PQEX", min_sessions=3) is None
    assert relative_volume(900, NOW, history(), symbol="PQEX", min_sessions=3, corporate_actions_verified=False) is None


def test_missing_minute_or_missing_recent_session_is_unknown():
    assert calculate(history()[1:]) is None
    # Older complete data cannot replace a missing November12 session.
    incomplete = session_bars("2026-11-11", 200) + session_bars("2026-11-10", 100) + session_bars("2026-11-09", 50)
    assert calculate(incomplete) is None


def test_future_current_other_symbol_and_extended_hours_are_ignored():
    additional = (
        session_bars("2026-11-13", 999999)
        + session_bars("2026-11-16", 999999)
        + [replace(bar, symbol="OTHER", volume=999999) for bar in history()]
        + [replace(history()[0], timestamp=datetime(2026, 11, 12, 14, 30, tzinfo=UTC), volume=999999)]
    )
    assert calculate(history() + additional) == pytest.approx(1.5)


def test_bar_received_after_asof_cannot_fill_historical_gap():
    bars = history()
    bars[0] = replace(bars[0], received_at=NOW + timedelta(seconds=1))
    assert calculate(bars) is None


def test_identical_duplicates_are_deduplicated_but_revisions_are_unknown():
    assert calculate(history() + [replace(history()[0], received_at=history()[0].received_at + timedelta(seconds=1))]) == 1.5
    assert calculate(history() + [replace(history()[0], volume=999)]) is None


def test_dst_changes_use_elapsed_market_minutes_instead_of_utc_clock_time():
    # March9 has switched to EDT; March6/5 still use EST.
    bars = session_bars("2026-03-09", 300, hour=13) + session_bars("2026-03-06", 200) + session_bars("2026-03-05", 100)
    now = datetime(2026, 3, 10, 13, 33, tzinfo=UTC)
    assert calculate(bars, now=now) == pytest.approx(1.5)


def test_holidays_are_not_required_as_historical_sessions():
    # Thanksgiving November26 is closed. November27 is a13:00 close, but its
    # first three minutes remain comparable to November30's first three.
    bars = session_bars("2026-11-27", 300) + session_bars("2026-11-25", 200) + session_bars("2026-11-24", 100)
    assert calculate(bars, now=datetime(2026, 11, 30, 14, 33, tzinfo=UTC)) == pytest.approx(1.5)


def test_short_prior_sessions_are_excluded_when_they_cannot_reach_elapsed_minute():
    #14:00 ET is270 minutes after open. Nov27 closed at13:00, so use Nov25/24/23.
    bars = (
        session_bars("2026-11-27", 99999, minutes=210)
        + session_bars("2026-11-25", 300, minutes=270)
        + session_bars("2026-11-24", 200, minutes=270)
        + session_bars("2026-11-23", 100, minutes=270)
    )
    assert calculate(bars, current=81_000, now=datetime(2026, 11, 30, 19, 0, tzinfo=UTC)) == pytest.approx(1.5)


def test_after_early_close_and_holiday_current_times_are_unknown():
    assert calculate(now=datetime(2026, 11, 27, 18, 1, tzinfo=UTC)) is None
    assert calculate(now=datetime(2026, 11, 26, 14, 33, tzinfo=UTC)) is None


@pytest.mark.parametrize("value", [-1, 1.5, True, float("nan")])
def test_invalid_current_volume_is_unknown(value):
    assert calculate(current=value) is None


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_invalid_historical_volume_is_unknown(value):
    # Negative volume cannot pass Bar construction; mutation simulates an
    # unvalidated import without weakening the shared immutable model.
    bars = history()
    object.__setattr__(bars[0], "volume", value)
    assert calculate(bars) is None


def test_zero_historical_denominator_and_inexact_asof_are_unknown():
    assert calculate([replace(bar, volume=0) for bar in history()]) is None
    assert calculate(now=NOW + timedelta(seconds=1)) is None
    assert calculate(now=NOW.replace(tzinfo=None)) is None
    assert calculate(now=NOW.replace(minute=30)) is None


def test_default_requires_twenty_comparable_sessions():
    assert relative_volume(900, NOW, history(), symbol="PQEX", corporate_actions_verified=True) is None


def test_default_twenty_session_window_computes_when_complete():
    dates = ["2026-11-12", "2026-11-11", "2026-11-10", "2026-11-09", "2026-11-06",
             "2026-11-05", "2026-11-04", "2026-11-03", "2026-11-02", "2026-10-30",
             "2026-10-29", "2026-10-28", "2026-10-27", "2026-10-26", "2026-10-23",
             "2026-10-22", "2026-10-21", "2026-10-20", "2026-10-19", "2026-10-16"]
    # Before November's DST change, regular-session open was13:30 UTC.
    bars = [bar for day in dates for bar in session_bars(day, 100, hour=13 if day < "2026-11-01" else 14)]
    assert relative_volume(900, NOW, bars, symbol="PQEX", corporate_actions_verified=True) == 3.0


@pytest.mark.parametrize("count", [0, -1, 1.5, True])
def test_invalid_session_count_is_configuration_error(count):
    with pytest.raises(ValueError):
        relative_volume(900, NOW, history(), symbol="PQEX", min_sessions=count, corporate_actions_verified=True)
