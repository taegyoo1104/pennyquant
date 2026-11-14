"""정규장 확정 1분봉에서 5분·15분봉과 기술 지표를 계산한다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from ..models import Bar, utc

NY = ZoneInfo("America/New_York")


@lru_cache(maxsize=16)
def calendar(year: int):
    import exchange_calendars as xcals
    return xcals.get_calendar("XNYS", start=f"{year}-01-01", end=f"{year}-12-31")


def session_bounds(now: datetime) -> tuple[datetime, datetime] | None:
    """휴장·서머타임·조기 폐장을 반영하며 조회 실패는 미확인으로 처리한다."""
    day = utc(now).astimezone(NY).date()
    try:
        cal = calendar(day.year)
        if not cal.is_session(str(day)):
            return None
        return cal.session_open(str(day)).to_pydatetime(), cal.session_close(str(day)).to_pydatetime()
    except (ValueError, KeyError, TypeError, ImportError, OverflowError):
        return None


def available_minutes(bars: list[Bar], now: datetime, symbol: str | None = None) -> list[Bar]:
    """실제 수신 이후의 봉만 선택한다. 충돌하는 동일 시각 봉은 사용하지 않는다."""
    now = utc(now)
    bounds = session_bounds(now)
    if bounds is None:
        return []
    opened, closed = bounds
    candidates: dict[datetime, Bar] = {}
    conflicts = set()
    symbols = {b.symbol for b in bars} if symbol is None else {symbol}
    if len(symbols) != 1:
        return []
    for bar in bars:
        if bar.symbol not in symbols:
            continue
        timestamp, received = utc(bar.timestamp), utc(bar.received_at)
        if not (opened < timestamp <= closed and timestamp <= received <= now):
            continue
        if timestamp.second or timestamp.microsecond:
            continue
        previous = candidates.get(timestamp)
        if previous and (previous.open, previous.high, previous.low, previous.close, previous.volume) != (
                bar.open, bar.high, bar.low, bar.close, bar.volume):
            conflicts.add(timestamp)
        elif previous is None or bar.received_at < previous.received_at:
            candidates[timestamp] = bar
    return [candidates[t] for t in sorted(candidates) if t not in conflicts]


def aggregate_bars(bars: list[Bar], minutes: int, now: datetime, *, symbol: str | None = None) -> list[Bar]:
    """장 시작 기준 버킷의 모든 1분봉이 확정·수신된 경우에만 출력한다."""
    if minutes not in (5, 15):
        raise ValueError("집계 주기는 5분 또는 15분이어야 합니다")
    bounds = session_bounds(now)
    if bounds is None:
        return []
    opened, closed = bounds
    lookup = {bar.timestamp: bar for bar in available_minutes(bars, now, symbol)}
    result = []
    end = opened + timedelta(minutes=minutes)
    while end <= min(closed, utc(now)):
        times = [end - timedelta(minutes=minutes-i-1) for i in range(minutes)]
        selected = [lookup[t] for t in times if t in lookup]
        if len(selected) == minutes:
            result.append(Bar(selected[0].symbol, end, max(b.received_at for b in selected),
                              selected[0].open, max(b.high for b in selected), min(b.low for b in selected),
                              selected[-1].close, sum(b.volume for b in selected)))
        end += timedelta(minutes=minutes)
    return result


def contiguous(bars: list[Bar], minutes: int) -> bool:
    return bool(bars) and all(b.timestamp - a.timestamp == timedelta(minutes=minutes)
                             for a, b in zip(bars, bars[1:]))


def ema(values: list[float], period: int = 9) -> float | None:
    if period < 1 or len(values) < period:
        return None
    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def atr(bars: list[Bar], period: int = 14) -> float | None:
    if period < 1 or len(bars) < period + 1:
        return None
    ranges = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close)) for a, b in zip(bars, bars[1:])]
    result = sum(ranges[:period]) / period
    for value in ranges[period:]:
        result = (result * (period - 1) + value) / period
    return result


def session_vwap(bars: list[Bar], now: datetime, *, through: datetime | None = None) -> float | None:
    selected = available_minutes(bars, now)
    bounds = session_bounds(now)
    if not selected or not bounds:
        return None
    if through:
        selected = [bar for bar in selected if bar.timestamp <= utc(through)]
    if not selected or selected[0].timestamp != bounds[0] + timedelta(minutes=1) or not contiguous(selected, 1):
        return None
    volume = sum(bar.volume for bar in selected)
    return sum((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in selected) / volume if volume else None


@dataclass(frozen=True)
class Indicators:
    five: tuple[Bar, ...]
    fifteen: tuple[Bar, ...]
    vwap: float | None
    ema9: float | None
    atr14: float | None
    volume_ratio: float | None
    cumulative_volume: int
    dollar_volume: float
    previous_vwap: float | None = None
    vwap_through: datetime | None = None
    previous_vwap_through: datetime | None = None
    calculated_at: datetime | None = None

    def to_dict(self) -> dict:
        return {"vwap": self.vwap, "previous_vwap": self.previous_vwap, "ema9": self.ema9, "atr14": self.atr14,
                "volume_ratio": self.volume_ratio, "cumulative_volume": self.cumulative_volume,
                "dollar_volume": self.dollar_volume, "five_count": len(self.five), "fifteen_count": len(self.fifteen),
                "last_closed_at": self.five[-1].timestamp.isoformat() if self.five else None,
                'vwap_through':self.vwap_through.isoformat() if self.vwap_through else None,
                'previous_vwap_through':self.previous_vwap_through.isoformat() if self.previous_vwap_through else None,
                'calculated_at':self.calculated_at.isoformat() if self.calculated_at else None}


def indicators(bars: list[Bar], now: datetime) -> Indicators:
    five = aggregate_bars(bars, 5, now)
    fifteen = aggregate_bars(bars, 15, now)
    through = five[-1].timestamp if five else None
    selected = [bar for bar in available_minutes(bars, now) if through and bar.timestamp <= through]
    continuous = contiguous(five, 5)
    previous_volume = sum(bar.volume for bar in five[-6:-1]) / 5 if len(five) >= 6 else 0
    return Indicators(tuple(five), tuple(fifteen), session_vwap(bars, now, through=through) if through else None,
                      ema([bar.close for bar in five]) if continuous else None,
                      atr(five) if continuous else None,
                      five[-1].volume / previous_volume if previous_volume and continuous else None,
                      sum(bar.volume for bar in selected), sum(bar.close * bar.volume for bar in selected),
                      session_vwap(bars, now, through=five[-2].timestamp) if len(five) >= 2 else None,
                      through,five[-2].timestamp if len(five)>=2 else None,utc(now))
