"""이벤트가 없는 시세 갱신은 무료이며, 새 검증 사건만 한 번 분석한다."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import httpx
import pytest

from sessionquant.macro.analysis import BudgetedAnalyzer
from sessionquant.macro.context_analysis import MacroContextAnalyzer
from test_stage07_audit import repo,NOW,macro,response

BODY='The agency released the employment report and confirmed a change in payrolls.'


def snapshot(change='-1.0'):
    window={'start_at':(NOW-timedelta(minutes=15)).isoformat(),'event_at':NOW.isoformat(),'available_at':NOW.isoformat(),
        'quality_status':'GOOD','sample_count':16,'required_samples':16,'input_ids':[f'point:{i}' for i in range(16)],
        'pct_change':change,'volatility_pct':'0.05'}
    return {'id':'context:one','context_at':NOW.isoformat(),'known_at':NOW.isoformat(),'reasons':[],
        'official_daily_indicators':[],'intraday_market_proxies':[{'id':'IEF:metrics','symbol':'IEF','source':'SYNTHETIC_FIXTURE',
        'event_at':NOW.isoformat(),'available_at':NOW.isoformat(),'valid_until':(NOW+timedelta(minutes=10)).isoformat(),
        'quality_status':'GOOD','latency_class':'REALTIME','windows':{'15':window}}]}


def context_facts(request,**changes):
    payload=json.loads(request.content);value=json.loads(payload['messages'][1]['content'])
    article=value['verified_news'][0]
    return {'event_type':'EMPLOYMENT','status':'CONFIRMED','direction':'NEGATIVE','importance':'HIGH',
        'affected_sectors':['technology'],'possible_transmission_channels':['금융 여건을 통한 영향 가능성'],
        'alternative_explanations':['다른 수급 요인의 영향 가능성'],'uncertainty_ko':'인과관계는 검증되지 않았습니다.',
        'reason_ko':'검증된 발표와 ETF 관측을 구분합니다.','requires_verification':True,
        'evidence_news_quotes':[{'article_id':article['id'],'paragraph_id':article['paragraphs'][0]['id'],
            'quote':'released the employment report'}],
        'evidence_market_data_ids':[value['reference_quotes'][0]['source_ids'][0]]}|changes


async def test_default_off_and_one_hundred_ordinary_updates_make_zero_requests(repo):
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:calls.append(request))) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        off=MacroContextAnalyzer(repo,analyzer,clock=lambda:NOW)
        assert (await off.analyze(snapshot()))['status']=='DISABLED'
        active=MacroContextAnalyzer(repo,analyzer,{'enabled':True},clock=lambda:NOW)
        for _ in range(100):await active.analyze(snapshot(change='0.01'),[macro(body=BODY)])
    assert calls==[] and repo.all('llm_usage')==[] and repo.all('llm_calls')==[]
    assert len(repo.all('macro_context_analysis'))==1


async def test_single_verified_event_trace_cache_repetition_and_new_evidence(repo):
    calls=[];clock=[NOW]
    def handle(request):calls.append(request);return httpx.Response(200,json=response(context_facts(request)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:clock[0])
        context=MacroContextAnalyzer(repo,analyzer,{'enabled':True},clock=lambda:clock[0])
        article=macro(body=BODY)
        first=await context.analyze(snapshot(),[article])
        assert first['status']=='SUCCEEDED' and not first['force_liquidation'] and not first['entry_eligible']
        assert len(first['llm_call_ids'])==1 and len(calls)==1
        repeated=await context.analyze(snapshot(),[article,replace(article,url='https://www.bls.gov/duplicate'),article])
        assert repeated['cached'] and len(calls)==1
        clock[0]+=timedelta(seconds=1)
        changed=replace(article,body=BODY+' The report includes additional employment data.')
        second=await context.analyze(snapshot(),[changed])
        assert second['status']=='SUCCEEDED' and len(calls)==2
        assert second['reference_event_id']==first['reference_event_id']
        rows=[row for row in repo.all('llm_calls') if row['network_attempted']]
        assert all(row['trigger_reason']=='NUMERIC_OUTLIER' for row in rows)
        assert rows[0]['reference_event_id']==first['reference_event_id']
        assert rows[0]['news_ids']==[article.id] and rows[0]['market_snapshot_ids']==['IEF:metrics']
        assert rows[0]['prompt_version']=='macro-context-facts-v1'
        assert repo.all('llm_usage')[0]['llm_call_id']==first['llm_call_ids'][0]


@pytest.mark.parametrize('case',['delayed','unknown_time','samples','future_daily','title_only','wrong_topic','stale_news','empty_body','missing_key'])
async def test_bad_quality_never_reaches_context_model(repo,case):
    value=snapshot();article=macro(body=BODY)
    if case=='delayed':value['intraday_market_proxies'][0]['latency_class']='DELAYED'
    if case=='unknown_time':value['intraday_market_proxies'][0]['event_at']=None
    if case=='samples':value['intraday_market_proxies'][0]['windows']['15']['sample_count']=2
    if case=='future_daily':value['official_daily_indicators']=[{'observation_date':'2026-11-14'}]
    if case=='title_only':article=replace(article,body='',official=False,discovery_source='gdelt')
    if case=='wrong_topic':article=replace(article,topic='technology_regulation',title='Gardening advice',body='A gardening guide with no relevant macro information.')
    if case=='stale_news':article=replace(article,published_at=NOW-timedelta(hours=25))
    if case=='empty_body':article=replace(article,body='')
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:calls.append(request))) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='' if case=='missing_key' else 'fixture',client=http,clock=lambda:NOW)
        result=await MacroContextAnalyzer(repo,analyzer,{'enabled':True},clock=lambda:NOW).analyze(value,[article])
    assert not result['llm_requested'] and calls==[] and repo.all('llm_usage')==[]


@pytest.mark.parametrize('case,code',[('data_id','EVIDENCE_QUOTE_MISMATCH'),('quote','EVIDENCE_QUOTE_MISMATCH'),
    ('paragraph','EVIDENCE_QUOTE_MISMATCH'),('invented_numeric','UNVERIFIED_NUMERIC_CLAIM'),('schema','SCHEMA_MISMATCH')])
async def test_context_output_requires_original_data_ids_and_body_evidence(repo,case,code):
    def handle(request):
        value=context_facts(request)
        if case=='data_id':value['evidence_market_data_ids']=['invented:data']
        if case=='quote':value['evidence_news_quotes'][0]['quote']='This quotation is not in the source'
        if case=='paragraph':value['evidence_news_quotes'][0]['paragraph_id']='p9999'
        if case=='invented_numeric':value['reason_ko']='TLT 하락은 금리 99bp 상승입니다.'
        if case=='schema':value['direction']='BUY'
        return httpx.Response(200,json=response(value))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        result=await MacroContextAnalyzer(repo,analyzer,{'enabled':True},clock=lambda:NOW).analyze(snapshot(),[macro(body=BODY)])
    assert result['status']=='NO_LLM_RESULT' and result['validation_error_code']==code
    assert not result['entry_eligible'] and not result['force_liquidation']
    assert len(repo.all('llm_usage'))==1


async def test_context_429_is_not_retried_and_numeric_assessment_remains_independent(repo):
    calls=[]
    def handle(request):calls.append(request);return httpx.Response(429,json={'error':{'code':'rate_limit_exceeded'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        context=MacroContextAnalyzer(repo,analyzer,{'enabled':True},clock=lambda:NOW)
        original=deepcopy(snapshot())
        result=await context.analyze(original,[macro(body=BODY)])
        again=await context.analyze(original,[macro(body=BODY)])
        assert context._measurements(original,NOW)[1]
    assert result['status']=='NO_LLM_RESULT' and again['skipped_precheck_reason']=='EVENT_COOLDOWN'
    assert len(calls)==1 and original==snapshot() and len(repo.all('llm_usage'))==1
