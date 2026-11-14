"""분할 비교 가능성과 완전한 동일 경과 분 이력을 요구하는 RVOL 계산."""
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
    # 기존 거래일·위험 관리와 같은 거래소 달력을 사용한다.
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
    observed_at: datetime | None = None,
) -> float | None:
    """현재 누적 거래량을 직전 동일 경과 분의 평균 누적 거래량으로 나눈다.

    ``current_time``은 확정 정규장 분 경계다. 현재 누적치는 개장부터 해당 경계까지
    빠짐없이 포함해야 한다. 해당 경과 분까지 도달하는 직전 ``min_sessions`` 거래일을
    선택하고, 누락된 최근 거래일을 더 오래된 완전한 날로 대체하지 않는다.

    ``observed_at``은 이 값을 실제 사용할 때의 확인 가능 시각이다. 생략하면 기존처럼
    ``current_time``을 쓴다. 종료 경계 뒤에 수신한 과거 페이지도 실제 수신 후에만
    사용할 수 있다. 호출자는 기업행동/분할 전후 거래량 비교 근거를 별도로 확인한다.
    """
    if not isinstance(min_sessions, int) or isinstance(min_sessions, bool) or min_sessions < 1:
        raise ValueError("min_sessions must be a positive integer")
    if corporate_actions_verified is not True or not _integer_volume(current_cumulative) or not symbol.strip():
        return None
    try:
        now = utc(current_time)
        known = utc(observed_at) if observed_at is not None else now
        if known < now:
            return None
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
        if bar_day not in selected_dates or received > known or received < timestamp:
            continue
        existing = observed[bar_day].get(timestamp)
        if existing is not None:
            # 동일 값의 반복 수신만 합친다. 충돌한 정정본을 임의 선택하지 않는다.
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
