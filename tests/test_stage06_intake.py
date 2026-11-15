"""뉴스 경계·독립 주기·과거 시각·공식 일정·오류 대기의 회귀 검증."""
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from types import SimpleNamespace
import json

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.models import NewsEvent
from sessionquant.news.intake import company_reason,time_reason,archive,age_bucket
from sessionquant.news.schedule import NewsSchedule
from sessionquant.macro.models import MacroArticle,EconomicEvent
from sessionquant.macro.service import NewsPipeline
from sessionquant.macro.sources import GDELTClient
from sessionquant.macro.calendar import parse_calendar
from sessionquant.macro.context import build_context
from sessionquant.session_config import load_session_config
from sessionquant.collectors.rss_collector import RSSCollector
from sessionquant.collectors.alpaca_news import AlpacaNewsClient

NOW=datetime(2026,11,13,16,tzinfo=timezone.utc)

@pytest.fixture
def repo(tmp_path):
    r=Repository(tmp_path/'news.db',backend='sqlite');yield r;r.close()

@pytest.mark.parametrize('hours,expected',[(0,''),(2,''),(24,''),(24+1/3600,'stale_article'),(72,'stale_article')])
def test_company_24h_boundary(hours,expected):
    assert time_reason(NOW-timedelta(hours=hours),NOW,NOW,24)==expected

@pytest.mark.parametrize('value',[None,'2026-11-13T16:00:00','2026-11-13T17:00:00Z'])
def test_missing_naive_future_publication_never_becomes_receipt_time(value):
    assert time_reason(value,NOW,NOW,72) in {'published_at_unknown','invalid_timestamp'}


def test_precise_company_blocks_and_2015_archive_remains(repo):
    event=NewsEvent('alpaca:1',('AAPL',),'Results',NOW-timedelta(minutes=2),NOW,'Benzinga','https://www.benzinga.com/article','Full source article.',True,False)
    assert company_reason(event,'MSFT','alpaca',False,NOW)=='ticker_not_in_article_symbols'
    assert company_reason(replace(event,source=''),'AAPL','alpaca',False,NOW)=='source_missing'
    assert company_reason(replace(event,source='alpaca'),'AAPL','alpaca',False,NOW)=='publisher_missing'
    assert company_reason(replace(event,body=''),'AAPL','alpaca',False,NOW)=='article_body_missing'
    old={'id':'alpaca:old','title':'Old story','published_at':'2015-01-01T00:00:00Z','received_at':NOW.isoformat(),'body':'archive'}
    a=archive(repo,old,provider='alpaca',reason='stale_article');b=archive(repo,{**old,'received_at':(NOW+timedelta(minutes=1)).isoformat()},provider='alpaca',reason='stale_article')
    assert b['duplicate'] and b['first_available_at']==a['first_available_at']
    assert len(repo.all('news_ingestions'))==2 and not b['eligible_for_llm']
    assert age_bucket(old['published_at'],NOW)=='과거'


def test_retry_after_and_independent_shared_state_survive_restart(repo):
    one=NewsSchedule(repo,clock=lambda:NOW,jitter=lambda:0)
    two=NewsSchedule(repo,clock=lambda:NOW,jitter=lambda:0)
    one.begin('gdelt');two.begin('bls')
    response=httpx.Response(429,headers={'Retry-After':'7200'},request=httpx.Request('GET','https://api.gdeltproject.org/'))
    one.failure('gdelt',httpx.HTTPStatusError('rate',request=response.request,response=response))
    two.success('bls',2)
    restored=NewsSchedule(repo,clock=lambda:NOW)
    assert not restored.due('gdelt',0,NOW+timedelta(seconds=7199))
    assert restored.due('gdelt',0,NOW+timedelta(seconds=7200))
    assert restored.state['bls']['successes']==1 and restored.state['gdelt']['http_429']==1


async def test_gdelt_429_stops_topics_no_retry_and_other_sources_are_independent(repo):
    calls=[]
    def handler(request):calls.append(request);return httpx.Response(429,headers={'Retry-After':'3600'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client=GDELTClient(client=http,repository=repo);client.limiter.interval=0
        assert await client.fetch(now=NOW)==[]
        assert await client.fetch(now=NOW+timedelta(minutes=16))==[]
        restored=GDELTClient(client=http,repository=repo)
        assert await restored.fetch(now=NOW+timedelta(minutes=30))==[]
    assert len(calls)==1


def ical(stamp='20261113T083000',tz=';TZID=America/New_York',status='CONFIRMED',sequence=0):
    return f'BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:jobs\nSUMMARY:Employment Situation\nDTSTART{tz}:{stamp}\nSEQUENCE:{sequence}\nSTATUS:{status}\nEND:VEVENT\nEND:VCALENDAR'.encode()


def test_calendar_dst_cancel_and_unknown_timezone(repo):
    url='https://www.bls.gov/schedule/news_release/bls.ics'
    known=NOW-timedelta(hours=4)
    events,issues=parse_calendar(ical(),url,known)
    assert not issues and events[0].scheduled_at.hour==13 and events[0].verified
    assert events[0].blackout(datetime(2026,11,13,13,30,tzinfo=timezone.utc))
    late,_=parse_calendar(ical(),url,NOW)
    assert not late[0].blackout(datetime(2026,11,13,13,30,tzinfo=timezone.utc))
    cancelled,_=parse_calendar(ical(status='CANCELLED',sequence=1),url,NOW)
    assert events[0].logical_id==cancelled[0].logical_id and events[0].id!=cancelled[0].id
    assert not cancelled[0].blackout(events[0].scheduled_at)
    assert not parse_calendar(ical(tz=''),url,NOW)[0]
    assert not parse_calendar(ical(stamp='20261101T013000'),url,NOW)[0]
    assert not parse_calendar(ical(stamp='20260308T023000'),url,NOW)[0]
    summer,_=parse_calendar(ical(stamp='20260710T083000'),url,NOW)
    assert summer[0].scheduled_at.hour==12


async def test_rss_304_and_bls_403_are_different_and_persist(repo,monkeypatch):
    monkeypatch.setattr('sessionquant.collectors.rss_collector.now_utc',lambda:NOW)
    calls=[]
    xml=b'<rss><channel><item><title>Release</title><link>https://www.bls.gov/release</link><pubDate>Fri, 13 Nov 2026 15:00:00 GMT</pubDate><description>Actual text.</description></item></channel></rss>'
    def handler(request):
        calls.append(request)
        if request.url.path=='/blocked':return httpx.Response(403)
        if request.headers.get('if-none-match'):return httpx.Response(304)
        return httpx.Response(200,content=xml,headers={'Content-Type':'application/rss+xml','ETag':'v1'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rss=RSSCollector(client=http,repository=repo);rss.schedule.clock=lambda:NOW
        await rss.fetch('https://www.bls.gov/feed',source='BLS')
        rss._host_limiters['www.bls.gov'].interval=0
        assert await rss.fetch('https://www.bls.gov/feed',source='BLS')==[]
        with pytest.raises(httpx.HTTPStatusError):await rss.fetch('https://www.bls.gov/blocked',source='BLS')
        assert await rss.fetch('https://www.bls.gov/blocked',source='BLS')==[]
    assert len(calls)==3 and calls[1].headers['if-none-match']=='v1'


async def test_alpaca_desc_window_and_repeated_pagination_are_bounded(monkeypatch):
    monkeypatch.setattr('sessionquant.collectors.alpaca_news.now_utc',lambda:NOW)
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'news':[],'next_page_token':'repeat'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client=AlpacaNewsClient('fixture','fixture',client=http);client.limiter.interval=0
        await client.fetch(['AAPL'])
    assert len(calls)==2 and all(r.url.params['sort']=='desc' for r in calls)
    assert calls[0].url.params['start']==(NOW-timedelta(hours=24)).isoformat()
    assert calls[0].url.params['end']==(NOW-timedelta(minutes=15)).isoformat()


async def test_pipeline_independent_intervals_restore_and_background_rotation(repo):
    calls=[]
    class Source:
        status=[]
        def __init__(self,name):self.name=name
        async def fetch(self,**kw):calls.append(self.name);return []
        async def aclose(self):pass
    class Company(Source):
        events=[]
        async def refresh(self,symbols,now):calls.append(('company',tuple(symbols)));return []
    analyzer=SimpleNamespace(ledger=SimpleNamespace(snapshot=lambda now:{}))
    def pipeline():return NewsPipeline(repo,analyzer=analyzer,company_service=Company('company'),official_client=Source('official'),
        treasury_client=Source('treasury'),gdelt_client=Source('gdelt'),fred_client=Source('fred'),calendar_client=Source('calendar'),clock=lambda:NOW)
    first=pipeline()
    try:
        await first.refresh(['FOCUS'],held_symbols=['HELD'],allowlist=['A','B','C','D'],now=NOW)
        assert calls[0]==('company',('HELD','FOCUS','A','B'))
        counts=len(calls)
        await first.refresh(['FOCUS'],now=NOW+timedelta(seconds=179));assert len(calls)==counts
        await first.refresh(['FOCUS'],held_symbols=['HELD'],allowlist=['A','B','C','D'],now=NOW+timedelta(seconds=180))
        assert calls[-1]==('company',('HELD','FOCUS','C','D'))
        second=pipeline()
        try:
            before=len(calls);await second.refresh([],now=NOW+timedelta(seconds=181));assert len(calls)==before
            await second.refresh([],now=NOW+timedelta(seconds=900));assert calls.count('official')==2
            assert calls.count('calendar')==1 and calls.count('fred')==1
            await second.refresh([],now=NOW+timedelta(seconds=3600));assert calls.count('calendar')==2
        finally:await second.aclose()
    finally:await first.aclose()


def test_fred_without_publication_never_becomes_intraday_or_future_context(repo):
    repo.record('fred_reference_observations',{'id':'one','series_id':'DGS10','value':'4.30','observation_date':'2026-11-12',
        'received_at':NOW.isoformat(),'ingested_at':NOW.isoformat(),'published_at':None,'point_in_time_verified':False})
    snapshot=build_context(repo,NOW)
    assert snapshot['official_daily_indicators'][0]['intraday_tick'] is False
    assert snapshot['official_daily_indicators'][0]['eligible_for_market_inference'] is False
    assert snapshot['related_verified_news_ids']==[] and not snapshot['causal_inference_verified']
    assert build_context(repo,NOW-timedelta(seconds=1))['official_daily_indicators']==[]


def test_old_yaml_maps_explicit_interval_and_new_option_wins(tmp_path):
    path=tmp_path/'cfg.yaml';path.write_text('news_refresh_seconds: 600\ncompany_news_refresh_seconds: 180\n')
    config=load_session_config(path)
    assert config.company_news_refresh_seconds==180 and config.macro_news_refresh_seconds==600
    path.write_text('companies: {}\n');config=load_session_config(path)
    assert config.company_news_refresh_seconds==180 and config.macro_news_refresh_seconds==900
