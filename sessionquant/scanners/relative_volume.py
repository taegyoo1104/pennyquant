"""Same elapsed-session-minute RVOL with complete, verified history.

The caller must attest that split/corporate-action handling makes all supplied
volumes comparable. This function cannot infer that property from OHLCV alone.
It intentionally returns None when exact comparable observations are missing.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache
import math
from zoneinfo import ZoneInfo

from ..models import Bar, utc


NEW_YORK = ZoneInfo("America/New_York")


@lru_cache(maxsize=16)
def _calendar(first_year: int, last_year: int):
    # Already a project dependency, shared with risk/session validation.
    import exchange_calendars as xcals
    return xcals.get_calendar("XNYS", start=f"{first_year}-01-01", end=f"{last_year}-12-31")


def _integer_volume(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def relative_volume(
    current_cumulative: int,
    current_time: datetime,
    history: list[Bar],
    *,
    symbol: str,
    min_sessions: int = 20,
    corporate_actions_verified: bool = False,
) -> float | None:
    """Current cumulative volume / mean of prior same-minute cumulatives.

    ``current_time`` is an exact completed regular-session minute boundary;
    ``current_cumulative`` must cover open through that boundary, excluding any
    current partial minute. The latest ``min_sessions`` prior calendar sessions
    long enough to reach that elapsed minute are used. Missing one of their
    required minute bars makes the result unknown: incomplete sessions are not
    silently replaced by older complete sessions.

    News/market timestamps are independent of the function's execution clock.
    Only historical bars received by ``current_time`` can contribute. The
    caller attests complete current-session coverage and corporate-action-safe,
    comparable raw/adjusted volumes via ``corporate_actions_verified=True``.
    """
    if not isinstance(min_sessions, int) or isinstance(min_sessions, bool) or min_sessions < 1:
        raise ValueError("min_sessions must be a positive integer")
    if corporate_actions_verified is not True or not _integer_volume(current_cumulative) or not symbol.strip():
        return None
    try:
        now = utc(current_time)
    except (TypeError, ValueError, AttributeError):
        return None
    if now.second or now.microsecond:
        return None
    day = now.astimezone(NEW_YORK).date()
    lookback = day - timedelta(days=max(90, min_sessions * 3 + 30))
    try:
        calendar = _calendar(lookback.year, day.year)
        if not calendar.is_session(str(day)):
            return None
        opened = calendar.session_open(str(day)).to_pydatetime()
        closed = calendar.session_close(str(day)).to_pydatetime()
        elapsed = (now - opened).total_seconds() / 60
        if elapsed < 1 or not elapsed.is_integer() or now > closed:
            return None
        elapsed_minutes = int(elapsed)
        sessions = calendar.sessions_in_range(str(lookback), str(day - timedelta(days=1)))
        selected: list[tuple[object, datetime]] = []
        for session in reversed(sessions):
            session_open = calendar.session_open(session).to_pydatetime()
            session_close = calendar.session_close(session).to_pydatetime()
            if (session_close - session_open).total_seconds() / 60 >= elapsed_minutes:
                selected.append((session.date(), session_open))
                if len(selected) == min_sessions:
                    break
    except (ValueError, TypeError, OverflowError, KeyError):
        return None
    if len(selected) < min_sessions:
        return None

    selected_dates = {session_day for session_day, _ in selected}
    observed: dict[object, dict[datetime, Bar]] = defaultdict(dict)
    conflicting: set[object] = set()
    for bar in history:
        if bar.symbol.upper() != symbol.strip().upper():
            continue
        try:
            timestamp, received = utc(bar.timestamp), utc(bar.received_at)
        except (TypeError, ValueError, AttributeError):
            continue
        bar_day = timestamp.astimezone(NEW_YORK).date()
        if bar_day not in selected_dates or received > now or received < timestamp:
            continue
        existing = observed[bar_day].get(timestamp)
        if existing is not None:
            # Identical repeated receipts can be deduplicated. Conflicting
            # revisions cannot be chosen without an explicit revision policy.
            original = (existing.open, existing.high, existing.low, existing.close, existing.volume)
            revised = (bar.open, bar.high, bar.low, bar.close, bar.volume)
            if original != revised:
                conflicting.add(bar_day)
        else:
            observed[bar_day][timestamp] = bar

    totals: list[int] = []
    for session_day, session_open in selected:
        if session_day in conflicting:
            return None
        session_total = 0
        for minute in range(1, elapsed_minutes + 1):
            timestamp = session_open + timedelta(minutes=minute)
            bar = observed[session_day].get(timestamp)
            if bar is None or not _integer_volume(bar.volume):
                return None
            session_total += bar.volume
        totals.append(session_total)
    try:
        average = sum(totals) / min_sessions
        result = current_cumulative / average if average > 0 else None
    except (OverflowError, ZeroDivisionError):
        return None
    return result if result is not None and math.isfinite(result) else None
