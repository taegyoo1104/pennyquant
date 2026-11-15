"""재시작해도 공급자별 요청 주기와 Retry-After 대기를 보존한다."""
from datetime import datetime,timedelta,timezone
from email.utils import parsedate_to_datetime
import random

import httpx
from ..models import utc

CONTROL='news_provider_schedule_v1'


class NewsSchedule:
    def __init__(self,repo=None,*,clock=None,jitter=None):
        self.repo=repo;self.clock=clock or (lambda:datetime.now(timezone.utc));self.jitter=jitter or random.random
        self.state=repo.get_control(CONTROL,{}) if repo else {}

    def save(self,name):
        if self.repo:
            latest=self.repo.get_control(CONTROL,{}) or {}
            latest[name]=self.state[name];self.state=latest
            self.repo.set_control(CONTROL,self.state)

    def due(self,name,interval,now=None):
        if self.repo:self.state=self.repo.get_control(CONTROL,{}) or {}
        now=utc(now or self.clock());row=self.state.get(name,{})
        if row.get('cooldown_until') and now<utc(row['cooldown_until']):return False
        return not row.get('last_attempt_at') or now>=utc(row['last_attempt_at'])+timedelta(seconds=interval)

    def begin(self,name,now=None):
        row=self.state.setdefault(name,{})
        row.update(last_attempt_at=utc(now or self.clock()).isoformat(),calls=row.get('calls',0)+1)
        self.save(name)

    def success(self,name,count=0,now=None):
        row=self.state.setdefault(name,{})
        row.update(last_success_at=utc(now or self.clock()).isoformat(),consecutive_errors=0,
            successes=row.get('successes',0)+1,last_count=count,status='FETCHED' if count else 'NO_NEW_ARTICLES',last_error=None)
        row.pop('cooldown_until',None);self.save(name)

    def failure(self,name,exc=None,*,code=None,now=None):
        now=utc(now or self.clock());row=self.state.setdefault(name,{})
        status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
        failures=row.get('consecutive_errors',0)+1
        wait=min(86400,60*2**min(failures-1,10))*(1+self.jitter()*.1)
        if status in {401,403,404}:wait=max(wait,3600)
        if status==429:wait=max(wait,600)
        until=now+timedelta(seconds=wait)
        if status is not None:
            value=exc.response.headers.get('Retry-After')
            if value:
                try:
                    seconds=float(value)
                    if seconds>=0:until=max(until,now+timedelta(seconds=seconds))
                except (ValueError,OverflowError):
                    try:until=max(until,utc(parsedate_to_datetime(value)))
                    except (ValueError,TypeError,AttributeError):pass
        row.update(status='PROVIDER_RATE_LIMITED' if status==429 else 'PROVIDER_UNAVAILABLE' if status in {401,403} else 'PROVIDER_HTTP_ERROR',
            cooldown_until=until.isoformat(),last_error=code or ('http_'+str(status) if status else type(exc).__name__),
            failures=row.get('failures',0)+1,consecutive_errors=failures)
        if status is not None:row['http_'+str(status)]=row.get('http_'+str(status),0)+1
        self.save(name)
