"""Daily candle dates are labels; their close is usable only after session end."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from ..market.bars import session_bounds
from ..models import utc

NY = ZoneInfo('America/New_York')


def completed(row):
    """Also works on persisted observations written before finality was recorded."""
    try:
        at, received, available = (utc(row[key]) for key in ('event_at', 'received_at', 'available_at'))
        bounds = session_bounds(at)
        return bool(bounds and at <= bounds[1] <= received <= available)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def previous_session_day(now):
    for days in range(1, 11):
        bounds = session_bounds(now - timedelta(days=days))
        if bounds:
            return bounds[0].astimezone(NY).date()
    return None


def previous_close(store, symbol, now):
    day = previous_session_day(now)
    rows = [row for row in store.rows(symbol, 'daily', now)
            if row['currency'] == 'USD' and row['price'] is not None
            and row['quality_status'] not in {'INVALID', 'UNAVAILABLE', 'REVISION_PENDING'}
            and utc(row['event_at']).astimezone(NY).date() == day]
    return max(rows, key=lambda row: row['available_at']) if rows else None
