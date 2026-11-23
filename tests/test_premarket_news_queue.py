from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.models import NewsEvent, jsonable
from sessionquant.news.analysis import NewsAnalysis
from sessionquant.news.analysis_queue import NewsAnalysisQueue, STATE, MAX_PENDING, session_phase
from sessionquant.news.intake import archive
from sessionquant.news.service import NewsService
from sessionquant.macro.analysis import BudgetedAnalyzer
from sessionquant.macro.budget import BudgetBlocked
from sessionquant.macro.models import MacroArticle
from sessionquant.macro.service import NewsPipeline

OPEN = datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc)
PRE = OPEN - timedelta(hours=2)


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path/'news.db', backend='sqlite')
    try:
        yield r
    finally:
        r.close()


class Clock:
    def __init__(self, now=PRE): self.now = now
    def __call__(self): return self.now


def event(index=1, symbol='AAPL', **kw):
    return replace(NewsEvent(f'alpaca:{index}', (symbol,), f'Company update {index}',
        PRE-timedelta(minutes=index), PRE, 'Publisher', f'https://example.com/{index}',
        f'Company announced a new product with updated details {index}.', True, False), **kw)


def enqueue(q, repo, e, now=PRE):
    r = archive(repo, jsonable(e), provider='alpaca', ticker=e.symbols[0])
    q.enqueue(r, kind='company', ticker=e.symbols[0], now=now)


def queue(repo, clock):
    return NewsAnalysisQueue(repo, clock=clock, version='test')


@pytest.mark.parametrize('now,phase', [
    (PRE, 'PREMARKET'), (OPEN, 'REGULAR'), (OPEN+timedelta(hours=6,minutes=30), 'CLOSED'),
    (datetime(2026,11,21,16,tzinfo=timezone.utc), 'CLOSED'),
    (datetime(2026,11,26,16,tzinfo=timezone.utc), 'CLOSED'),  # Thanksgiving
    (datetime(2026,11,27,18,tzinfo=timezone.utc), 'CLOSED'),  # early close
    (datetime(2026,7,6,13,30,tzinfo=timezone.utc), 'REGULAR'),  # daylight saving
])
def test_exchange_clock(now, phase):
    assert session_phase(now) == phase


async def test_premarket_persists_then_open_drains_without_new_collection(repo):
    clock=Clock();q=queue(repo,clock);calls=[]
    enqueue(q,repo,event())
    async def analyze(a,item,now): calls.append((a.id,now)); return ''
    await q.drain(analyze)
    assert calls == [] and q.snapshot()['pending']==1
    clock.now=OPEN
    restored=queue(repo,clock)
    await restored.drain(analyze)
    assert calls==[('alpaca:1',OPEN)]
    assert restored.snapshot()['pending']==0
    enqueue(restored,repo,event(),OPEN)
    assert restored.snapshot()['pending']==0  # finished identities survive restart


async def test_batch_priority_newest_and_persisted_cadence(repo):
    clock=Clock();q=queue(repo,clock);calls=[]
    for e in (event(1,'OTHER'),event(2,'FOCUS'),event(3,'HELD'),event(4,'HELD'),event(5,'OTHER')):
        enqueue(q,repo,e)
    async def analyze(a,item,now): calls.append(a.id); return ''
    clock.now=OPEN
    await q.drain(analyze,held=['HELD'],focused=['FOCUS'])
    assert calls==['alpaca:3','alpaca:4','alpaca:2']
    clock.now+=timedelta(seconds=299)
    await queue(repo,clock).drain(analyze)
    assert len(calls)==3
    clock.now+=timedelta(seconds=1)
    await queue(repo,clock).drain(analyze)
    assert calls[-2:]==['alpaca:1','alpaca:5']


def test_duplicate_text_without_merging_revisions_or_other_tickers(repo):
    clock=Clock();q=queue(repo,clock);e=event()
    enqueue(q,repo,e)
    enqueue(q,repo,replace(e,id='syndicated',url='https://other.com/story',body='  '+e.body.upper()+'  '))
    enqueue(q,repo,replace(e,id='revision',body=e.body+' Production was cancelled.'))
    enqueue(q,repo,replace(e,symbols=('MSFT',)))
    summary=q.snapshot()
    assert summary['pending']==3 and summary['counts']['duplicates']==1
    state=json.dumps(repo.get_control(STATE))
    assert e.body not in state and 'receipt_id' in state


async def test_expired_or_missing_input_is_not_sent(repo):
    clock=Clock();q=queue(repo,clock);enqueue(q,repo,event(published_at=OPEN-timedelta(hours=24,seconds=1)))
    enqueue(q,repo,event(2));repo.db.execute("DELETE FROM news_ingestions WHERE payload LIKE '%alpaca:2%'");repo.db.commit()
    calls=[]
    async def analyze(*args): calls.append(args)
    clock.now=OPEN;await q.drain(analyze)
    assert not calls and q.snapshot()['pending']==0
    assert q.snapshot()['counts']['expired']==1
    assert q.snapshot()['counts']['excluded']==1


async def test_budget_block_keeps_pending_and_stops_batch(repo):
    clock=Clock();q=queue(repo,clock)
    for i in range(3): enqueue(q,repo,event(i))
    calls=[]
    async def analyze(*args): calls.append(1); return 'llm_budget_limit'
    clock.now=OPEN;await q.drain(analyze)
    assert len(calls)==1 and q.snapshot()['pending']==3
    assert all(i['attempts']==0 for i in repo.get_control(STATE)['items'].values())
    clock.now+=timedelta(minutes=5);await queue(repo,clock).drain(analyze)
    assert len(calls)==1


async def test_failure_retries_are_bounded_across_restart(repo):
    clock=Clock();q=queue(repo,clock);enqueue(q,repo,event());calls=[]
    async def analyze(*args): calls.append(1); return 'llm_invalid_json'
    clock.now=OPEN
    for i in range(3):
        await queue(repo,clock).drain(analyze);clock.now+=timedelta(minutes=15)
    assert len(calls)==3 and q.snapshot()['pending']==0
    enqueue(q,repo,event(),clock.now)
    assert q.snapshot()['pending']==0


async def test_close_during_batch_stops_new_requests(repo):
    clock=Clock();q=queue(repo,clock)
    for i in range(3): enqueue(q,repo,event(i))
    calls=[]
    async def analyze(*args):
        calls.append(1);clock.now=OPEN+timedelta(hours=6,minutes=30);return ''
    clock.now=OPEN+timedelta(hours=6,minutes=29)
    await q.drain(analyze)
    assert len(calls)==1 and q.snapshot()['pending']==2


def test_queue_size_is_bounded_with_no_payload_copy(repo):
    clock=Clock();q=queue(repo,clock)
    for i in range(MAX_PENDING+5): enqueue(q,repo,event(i))
    assert q.snapshot()['pending']==MAX_PENDING
    assert q.snapshot()['counts']['capacity_excluded']==5
    assert len(json.dumps(repo.get_control(STATE)))<180000


async def test_wire_boundary_rejects_premarket_before_budget_or_http(repo):
    clock=Clock();requests=[]
    async def handler(request): requests.append(request);return httpx.Response(500)
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    analyzer=BudgetedAnalyzer(repo,api_key='test',client=client,clock=clock)
    analyzer.regular_session_only=True
    with pytest.raises(BudgetBlocked) as e:
        await analyzer._adapters['primary'].post('https://api.openai.com/v1/chat/completions',json={})
    assert e.value.code=='MARKET_CLOSED'
    assert not requests and repo.get_control('session_llm_budget_v1',{})=={}
    await client.aclose()


class Empty:
    status=[]
    async def fetch(self,*args,**kwargs): return []
    async def aclose(self): pass


class Analyzer:
    def __init__(self,clock):
        self.clock=clock;self.calls=[];self.max_age_hours=24
        self.ledger=SimpleNamespace(snapshot=lambda now:{})
    async def analyze(self,e,ticker,**kwargs):
        self.calls.append(('company',e.id))
        return NewsAnalysis(ticker,e,analyzed_at=kwargs['now'],reason='확인')
    async def analyze_macro(self,a,**kwargs):
        self.calls.append(('macro',a.id))
        return {'id':'analysis:'+a.id,'article_id':a.id,'analyzed_at':kwargs['now'].isoformat(),
                'published_at':a.published_at.isoformat(),'status':'UNKNOWN','error':''}
    async def aclose(self): pass


async def test_real_pipeline_collects_both_types_before_open_and_does_not_call_context_llm(repo):
    clock=Clock();analyzer=Analyzer(clock);empty=Empty()
    company=NewsService({'AAPL':{'cik':'1'}},analyzer=analyzer,repository=repo,clock=clock,include_alpaca=False,
        sec_client=empty,document_client=empty,rss_collector=empty,company_resolver=empty)
    async def refresh(symbols,now): return [await company._analyze(event(), 'AAPL', 'alpaca', False, now)]
    company.refresh=refresh
    a=MacroArticle('https://www.federalreserve.gov/story','Policy update','Official policy details are available.',
        'Federal Reserve',PRE-timedelta(minutes=1),PRE,official=True)
    class Official(Empty):
        async def fetch(self): return [a]
    pipeline=NewsPipeline(repo,analyzer=analyzer,company_service=company,official_client=Official(),
        treasury_client=empty,gdelt_client=empty,fred_client=empty,clock=clock,regular_session_analysis=True,
        company_refresh_seconds=86400,macro_refresh_seconds=86400)
    context_calls=[]
    pipeline.context_analyzer.settings=SimpleNamespace(enabled=True)
    async def context(*args,**kwargs): context_calls.append(1)
    pipeline.context_analyzer.analyze=context
    status=await pipeline.refresh(['AAPL'],held_symbols=['AAPL'])
    assert status['analysis_queue']['pending']==2 and not analyzer.calls and not context_calls
    assert repo.recent('news_ingestions',limit=10) and not repo.recent('news_analysis',limit=10)
    assert not repo.recent('macro_analysis',limit=10)
    clock.now=OPEN
    status=await pipeline.refresh(['AAPL'],held_symbols=['AAPL'])
    assert analyzer.calls==[('company','alpaca:1'),('macro',a.id)]
    assert status['analysis_queue']['pending']==0 and context_calls==[1]
    assert repo.recent('news_analysis',limit=10) and repo.recent('macro_analysis',limit=10)
    await pipeline.aclose()


def test_dashboard_distinguishes_waiting_from_stale_records(repo):
    from sessionquant.dashboard.news_queue_panel import queue_summary
    clock=Clock();q=queue(repo,clock);enqueue(q,repo,event())
    status={'analysis_queue':q.snapshot()}
    summary=queue_summary(status,PRE)
    assert summary['title']=='장전 뉴스 준비' and summary['pending']==1
    assert 'AI 분석은 개장 후' in summary['detail']
    assert queue_summary(status,OPEN)['title']=='마지막 뉴스 준비 기록'


async def test_gdelt_queue_restores_metadata_and_refetches_body_at_open(repo):
    clock=Clock();analyzer=Analyzer(clock);empty=Empty()
    article=MacroArticle('https://example.com/policy','Policy update','Verified publisher body text.',
        'Publisher',PRE-timedelta(minutes=1),PRE,discovery_source='gdelt',
        publisher_verified=True,llm_use_allowed=True)
    receipt=archive(repo,article.to_dict(),provider='gdelt',body_allowed=False)
    pipeline=NewsPipeline(repo,analyzer=analyzer,company_service=empty,official_client=empty,
        treasury_client=empty,gdelt_client=empty,fred_client=empty,clock=clock,regular_session_analysis=True)
    await pipeline.enricher.aclose()
    refetched=[]
    class Enricher(Empty):
        async def enrich(self, metadata):
            assert metadata.body == ''
            refetched.append(metadata.id)
            return {},replace(article,received_at=clock.now)
    pipeline.enricher=Enricher()
    pipeline.analysis_queue.enqueue(receipt,kind='macro',now=PRE,fingerprint_raw=article.to_dict())
    await pipeline.analysis_queue.drain(pipeline._analyze_queued)
    assert not refetched and not analyzer.calls
    clock.now=OPEN
    await pipeline.analysis_queue.drain(pipeline._analyze_queued)
    assert refetched==[article.id] and analyzer.calls==[('macro',article.id)]
    assert pipeline.analysis_queue.snapshot()['pending']==0
    assert 'body' not in repo.get_record('news_ingestions',receipt['id'])['raw']
    assert repo.recent('macro_news',limit=1)[0]['body']==''
    await pipeline.aclose()
