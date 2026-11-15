"""공식 BLS·BEA iCalendar에서 명시한 발표 시간만 읽는다. 일자를 추측하지 않는다."""
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import re

import httpx

from ..models import utc
from ..collectors.common import RateLimiter,get_readonly
from .models import EconomicEvent,stable_hash
from .sources import official_url
from ..news.schedule import NewsSchedule

CALENDARS={'bls':'https://www.bls.gov/schedule/news_release/bls.ics',
    'bea':'https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics'}


def parse_calendar(content,source_url,known_at):
    if not official_url(source_url) or len(content)>2_000_000:raise ValueError('공식 일정 입력을 확인하세요.')
    text=content.decode('utf-8-sig','strict')
    if 'BEGIN:VCALENDAR' not in text:raise ValueError('공식 iCalendar 형식이 아닙니다.')
    text=re.sub(r'\r?\n[ \t]','',text);events=[];issues=[]
    for block in text.split('BEGIN:VEVENT')[1:][:2000]:
        properties={}
        for line in block.split('END:VEVENT')[0].splitlines():
            if ':' not in line:continue
            key,value=line.split(':',1);name=key.split(';')[0]
            if name in properties:raise ValueError('일정 속성이 중복됩니다.')
            properties[name]=(key,value)
        try:
            key,stamp=properties['DTSTART'];name=properties['SUMMARY'][1].replace('\\,',',').replace('\\n',' ')
            uid=properties['UID'][1];sequence=int(properties.get('SEQUENCE',('', '0'))[1])
            if stamp.endswith('Z'):scheduled=datetime.strptime(stamp,'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            else:
                zone=re.search(r'(?:^|;)TZID=([^;]+)',key)
                if not zone:raise ValueError('시간대 미확인')
                naive=datetime.strptime(stamp,'%Y%m%dT%H%M%S');tz=ZoneInfo(zone[1])
                # 존재하지 않거나 중복되는 DST 로컬 시각을 임의로 선택하지 않는다.
                candidates={naive.replace(tzinfo=tz,fold=f).astimezone(timezone.utc) for f in (0,1)
                    if naive.replace(tzinfo=tz,fold=f).astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)==naive}
                if len(candidates)!=1:raise ValueError('DST 경계 시각 미확인')
                scheduled=candidates.pop()
            status=properties.get('STATUS',('', 'CONFIRMED'))[1]
            if status not in {'CONFIRMED','TENTATIVE','CANCELLED'}:raise ValueError('일정 상태 미확인')
            high=bool(re.search(r'consumer price|employment situation|gross domestic product|personal income|PCE|FOMC',name,re.I))
            logical='calendar:'+stable_hash([source_url,uid])[:32]
            version=logical+':'+stable_hash([scheduled.isoformat(),sequence,status,name])[:16]
            events.append(EconomicEvent(version,name,scheduled,utc(known_at),source_url,'HIGH' if high else 'MEDIUM',True,
                logical_id=logical,revision=sequence,status=status,scheduled_local=scheduled.astimezone(ZoneInfo('America/New_York')).isoformat()))
        except (ValueError,TypeError,KeyError):issues.append({'code':'calendar_time_or_identity_unknown'})
    return events,issues


class EconomicCalendarClient:
    def __init__(self,repo=None,*,client=None,clock=None,feeds=None):
        self.repo=repo;self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.client=client or httpx.AsyncClient(timeout=15,follow_redirects=False);self.owns=client is None
        self.feeds=feeds if feeds is not None else CALENDARS
        self.schedule=NewsSchedule(repo,clock=self.clock);self.limiter=RateLimiter(1);self.status=[]

    async def fetch(self):
        self.status=[];events=[]
        for provider,url in self.feeds.items():
            key='calendar:'+provider
            if not self.schedule.due(key,3600):continue
            self.schedule.begin(key)
            try:
                response=await get_readonly(self.client,url,limiter=self.limiter,attempts=1)
                values,issues=parse_calendar(response.content,url,utc(self.clock()))
                events.extend(values);self.status.extend({'provider':provider,**item} for item in issues)
                self.schedule.success(key,len(values));self.status.append({'provider':provider,'code':'ok','count':len(values)})
            except (httpx.HTTPError,ValueError,TypeError) as exc:
                self.schedule.failure(key,exc)
                self.status.append({'provider':provider,'code':'calendar_unavailable',
                    'http_status':exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None})
        # 공식 문서에 날짜만 있는 FOMC/EIA/Treasury는 시간 확정 없이는 자동 생성하지 않는다.
        self.status.extend({'provider':p,'code':'verified_schedule_import_required'} for p in ('fed','eia','treasury'))
        return events

    async def aclose(self):
        if self.owns:await self.client.aclose()
