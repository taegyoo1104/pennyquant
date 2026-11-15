"""공급자별 주기·Retry-After·요청 점유를 원자적으로 보존한다."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import random
from uuid import uuid4

import httpx
from ..models import utc

CONTROL = 'news_provider_schedule_v1'


class NewsSchedule:
    def __init__(self, repo=None, *, clock=None, jitter=None):
        self.repo = repo
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.jitter = jitter or random.random
        self.state = repo.get_control(CONTROL, {}) or {} if repo else {}
        self._attempts = {}

    def _update(self, name, update):
        def merge(state):
            state = state or {}
            state[name] = update(dict(state.get(name, {})))
            return state
        self.state = self.repo.update_control(CONTROL, merge, {}) if self.repo else merge(self.state)

    @staticmethod
    def _due(row, interval, now):
        for key in ('cooldown_until', 'in_flight_until'):
            if row.get(key) and now < utc(row[key]):
                return False
        return not row.get('last_attempt_at') or now >= utc(row['last_attempt_at']) + timedelta(seconds=interval)

    def due(self, name, interval, now=None):
        if self.repo:
            self.state = self.repo.get_control(CONTROL, {}) or {}
        return self._due(self.state.get(name, {}), interval, utc(now or self.clock()))

    def claim(self, name, interval, now=None):
        """한 프로세스만 요청을 시작한다. 중단된 요청 점유는 5분 후 만료된다."""
        now = utc(now or self.clock())
        claimed = False
        token = uuid4().hex
        def update(row):
            nonlocal claimed
            if self._due(row, interval, now):
                claimed = True
                row.update(last_attempt_at=now.isoformat(), calls=row.get('calls', 0)+1,
                           attempt_id=token,
                           in_flight_until=(now+timedelta(seconds=300)).isoformat())
            return row
        self._update(name, update)
        if claimed:
            self._attempts[name] = token
        return claimed

    def begin(self, name, now=None):
        now = utc(now or self.clock())
        token = uuid4().hex
        def update(row):
            row.update(last_attempt_at=now.isoformat(), calls=row.get('calls', 0)+1, attempt_id=token)
            return row
        self._update(name, update)
        self._attempts[name] = token

    def success(self, name, count=0, now=None, *, minimum_wait=0):
        now = utc(now or self.clock())
        def update(row):
            if name in self._attempts and row.get('attempt_id') != self._attempts[name]:
                return row
            row.update(last_success_at=now.isoformat(), consecutive_errors=0,
                       successes=row.get('successes', 0)+1, last_count=count,
                       status='FETCHED' if count else 'NO_NEW_ARTICLES', last_error=None)
            if minimum_wait:
                row['cooldown_until'] = (now+timedelta(seconds=minimum_wait)).isoformat()
            else:
                row.pop('cooldown_until', None)
            row.pop('in_flight_until', None)
            return row
        self._update(name, update)

    def failure(self, name, exc=None, *, code=None, now=None):
        now = utc(now or self.clock())
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        def update(row):
            if name in self._attempts and row.get('attempt_id') != self._attempts[name]:
                return row
            failures = row.get('consecutive_errors', 0)+1
            wait = min(86400, 60*2**min(failures-1, 10))*(1+self.jitter()*.1)
            if status in {401, 403, 404}:
                wait = max(wait, 3600)
            if status == 429:
                wait = max(wait, 600)
            until = now+timedelta(seconds=wait)
            value = exc.response.headers.get('Retry-After') if status is not None else None
            if value:
                try:
                    seconds = float(value)
                    if math.isfinite(seconds) and seconds >= 0:
                        until = max(until, now+timedelta(seconds=seconds))
                except (ValueError, OverflowError):
                    try:
                        until = max(until, utc(parsedate_to_datetime(value)))
                    except (ValueError, TypeError, AttributeError, OverflowError):
                        pass
            if row.get('cooldown_until'):
                until = max(until, utc(row['cooldown_until']))
            row.update(status='PROVIDER_RATE_LIMITED' if status == 429 else
                       'PROVIDER_UNAVAILABLE' if status in {401, 403} else 'PROVIDER_HTTP_ERROR',
                       cooldown_until=until.isoformat(),
                       last_error=code or ('http_'+str(status) if status else type(exc).__name__),
                       failures=row.get('failures', 0)+1, consecutive_errors=failures)
            row.pop('in_flight_until', None)
            if status is not None:
                row['http_'+str(status)] = row.get('http_'+str(status), 0)+1
            return row
        self._update(name, update)
