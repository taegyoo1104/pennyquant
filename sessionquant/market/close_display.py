"""Bounded, display-only closing records; never used for order decisions."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .bars import session_bounds
from ..models import utc

VIEW = 'market_close_display_v1'
NY = ZoneInfo('America/New_York')
BENCHMARKS = ('SPY', 'QQQ', 'SOXX')


def completed_session(now):
    now = utc(now)
    for offset in range(11):
        bounds = session_bounds(now - timedelta(days=offset))
        if bounds and bounds[1] <= now:
            return bounds
    return None


def closed_market(now):
    bounds = session_bounds(now)
    # Midnight is not a new premarket session. Keep the prior close until 04:00 ET.
    return bounds is None or now >= bounds[1] or utc(now).astimezone(NY).hour < 4


def day_key(bounds):
    return bounds[0].astimezone(NY).date().isoformat()


def positive(value):
    if isinstance(value, bool):
        raise ValueError('invalid closing price')
    price = Decimal(str(value))
    if not price.is_finite() or price <= 0:
        raise ValueError('invalid closing price')
    return price


def daily_close(data, bounds, received):
    """Only same-session daily bars fetched after close; reject conflicting bars."""
    prior = completed_session(bounds[0])
    if not prior or received < bounds[1] or not isinstance(data, dict) or not isinstance(data.get('candles'), list):
        return None
    days = {day_key(bounds): set(), day_key(prior): set()}
    for row in data.get('candles', []):
        try:
            at = utc(row['timestamp'])
            if at > received or row.get('currency') != 'USD':
                continue
            key = at.astimezone(NY).date().isoformat()
            if key not in days:
                continue
            price = positive(row['closePrice'])
            low, high = positive(row['lowPrice']), positive(row['highPrice'])
            opened = positive(row['openPrice'])
            if not low <= min(price, opened) <= max(price, opened) <= high:
                return None
            days[key].add(price)
        except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
            return None
    if len(days[day_key(bounds)]) != 1 or len(days[day_key(prior)]) > 1:
        return None
    previous = next(iter(days[day_key(prior)]), None)
    return {'session_date': day_key(bounds), 'price': str(next(iter(days[day_key(bounds)]))),
            'previous_close': str(previous) if previous else None,
            'previous_session_date': day_key(prior), 'closed_at': bounds[1].isoformat(),
            'received_at': received.isoformat(), 'source': 'toss:daily:unadjusted'}


class CloseDisplay:
    def __init__(self, repo):
        self.repo = repo
        self.state = repo.get_control(VIEW, {}) or {}

    def capture(self, watchlist, now):
        bounds = session_bounds(now)
        if not bounds or not bounds[0] <= now < bounds[1]:
            return
        key = day_key(bounds)
        if self.state.get('session_date') != key:
            self.state = {'session_date': key, 'items': {}, 'focus': [], 'attempts': {}}
        # Preserve the last nonempty regular-session focus through late data gaps.
        focus = [deepcopy(row) for row in watchlist.get('items', []) if row.get('focused') is True][:10]
        if self.state.get('captured_at') and (now - utc(self.state['captured_at'])).total_seconds() < 60:
            return
        self.state.update(focus=focus or self.state.get('focus', []), captured_at=now.isoformat(), purpose='DISPLAY_ONLY')
        self.repo.set_control(VIEW, self.state)

    async def collect(self, read, now):
        bounds = session_bounds(now)
        if not bounds or not bounds[1] <= now <= bounds[1] + timedelta(minutes=10):
            return
        key = day_key(bounds)
        if self.state.get('session_date') != key:
            self.state = {'session_date': key, 'items': {}, 'focus': [], 'attempts': {}, 'purpose':'DISPLAY_ONLY'}
        targets = list(dict.fromkeys([*BENCHMARKS, *(r['ticker'] for r in self.state.get('focus', []) if r.get('ticker'))]))
        items, attempts = self.state.setdefault('items', {}), self.state.setdefault('attempts', {})
        due = [s for s in targets if s not in items and attempts.get(s, {}).get('count', 0) < 3
               and (not attempts.get(s) or (now - utc(attempts[s]['at'])).total_seconds() >= 60)]
        due.sort(key=lambda s: (attempts.get(s, {}).get('count', 0), targets.index(s)))
        for symbol in due[:2]:
            result = await read(symbol)
            if result is None:  # No capacity; retry on a later existing collector cycle.
                continue
            received = utc(result['received_at'])
            attempts[symbol] = {'count': attempts.get(symbol, {}).get('count', 0) + 1, 'at': received.isoformat()}
            item = daily_close(result.get('data'), bounds, received)
            if item:
                items[symbol] = item
        self.repo.set_control(VIEW, self.state)
