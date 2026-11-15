"""기사·실제 호출·검증·토큰/비용을 모의 응답과 격리 원장에서 대조한다."""
import asyncio
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.llm.audit import AuditTrail
from sessionquant.macro.analysis import BudgetedAnalyzer,BudgetLedger,BudgetBlocked
from sessionquant.macro.models import MacroArticle,LLMSettings
from sessionquant.models import NewsEvent

NOW=datetime(2026,11,13,16,tzinfo=timezone.utc)
BODY='The company reported quarterly revenue growth and a confirmed increase in earnings.'


@pytest.fixture
def repo(tmp_path):
    value=Repository(tmp_path/'audit.db',backend='sqlite');yield value;value.close()


def company(**changes):
    event=NewsEvent('rss:one',('TEST',),'Company reports quarterly results',NOW-timedelta(minutes=1),NOW,
                    'Company IR','https://issuer.test/release',BODY,True,False)
    return replace(event,**changes)


def facts(**changes):
    value={'ticker':'TEST','events':[{'type':'EARNINGS_BEAT','status':'CONFIRMED','company_impact':'POSITIVE',
        'shareholder_impact':'POSITIVE','importance':'MEDIUM','dilution_risk':False,'evidence':'reported quarterly revenue growth'}],
        'overall_sentiment':'POSITIVE','confidence':.9,'requires_verification':False,'missing_information':[],
        'reason':'실적 원문 확인'}
    return value|changes


def macro(**changes):
    return replace(MacroArticle('https://www.bls.gov/release','Employment report',BODY,'BLS',
                   NOW-timedelta(minutes=1),NOW,official=True,topic='inflation_and_employment'),**changes)


def macro_facts(**changes):
    return {'event_type':'EMPLOYMENT','status':'CONFIRMED','direction':'NEUTRAL','importance':'MEDIUM',
        'expected_duration':'INTRADAY','evidence':'reported quarterly revenue growth','uncertainty':'영향 미확인',
        'reason':'공식 원문 확인','affected_assets':['SPY'],'affected_sectors':[]}|changes


def response(value=None,*,usage=True):
    result={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value or facts())}}]}
    if usage:result['usage']={'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':40}}
    return result


async def test_company_call_usage_result_cache_restart_and_exact_payload_are_linked(repo):
    calls=[];clock=[NOW]
    def handle(request):calls.append(request);return httpx.Response(200,json=response(),headers={'x-request-id':'req-fixture'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture-key',client=http,clock=lambda:clock[0])
        first=await analyzer.analyze(company(),'TEST',source_kind='rss',primary_verified=True)
        assert first.entry_eligible and len(first.llm_call_ids)==1
        row=repo.all('llm_calls')[0];usage=repo.all('llm_usage')[0]
        assert row['status']=='SUCCEEDED' and row['network_attempted'] and row['provider_request_id']=='req-fixture'
        assert row['llm_call_id']==usage['llm_call_id']==first.adopted_llm_call_id
        assert usage['article_id']==row['article_id']==company().id
        assert usage['cost_status']=='TOKEN_BASED_ESTIMATE' and usage['provider_billed_cost_usd'] is None
        assert Decimal(usage['cost_usd'])==Decimal('0.000060')
        assert row['input_hash'] and row['prompt_hash'] and row['response_hash']
        from sessionquant.news.analysis import PROMPT_VERSION
        assert row['prompt_version']==row['normalized_result']['prompt_version']==first.prompt_version==PROMPT_VERSION
        assert row['normalized_result']['evidence_source_spans'][0]['paragraph_id']=='p0001'
        assert 'Authorization' not in json.dumps(row) and BODY not in json.dumps(row)
        sent=json.loads(calls[0].content)
        user=json.loads(sent['messages'][1]['content'])
        assert user['article_metadata']['article_id']==company().id
        assert user['article_data'].endswith(BODY) and user['article_metadata']['sent_content_hash']
        clock[0]+=timedelta(seconds=2)
        restarted=BudgetedAnalyzer(repo,api_key='fixture-key',client=http,clock=lambda:clock[0])
        cached=await restarted.analyze(company(),'TEST',source_kind='rss',primary_verified=True)
        assert cached.cached and cached.llm_call_ids==first.llm_call_ids and len(calls)==1
        assert cached.prompt_version==first.prompt_version
        assert len(repo.all('llm_usage'))==1
        assert repo.all('llm_calls')[-1]['status']=='CACHE_HIT' and not repo.all('llm_calls')[-1]['network_attempted']
        assert cached.to_dict()['analysis_id']==first.to_dict()['analysis_id']


@pytest.mark.parametrize('kind',['gdelt','stale','unverified','empty'])
async def test_prechecks_are_zero_request_records_not_zero_token_paid_usage(repo,kind):
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:calls.append(request))) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture-key',client=http,clock=lambda:NOW)
        value=macro()
        if kind=='gdelt':value=MacroArticle('https://publisher.test/news','Title only','','GDELT',None,NOW,discovery_source='gdelt')
        if kind=='stale':value=macro(published_at=NOW-timedelta(hours=73))
        if kind=='unverified':value=macro(url='https://unverified.test/article',official=False)
        if kind=='empty':value=macro(body='')
        result=await analyzer.analyze_macro(value)
        assert result['status']=='UNKNOWN' and not result['force_liquidation'] and calls==[]
        assert repo.all('llm_usage')==[]
        row=repo.all('llm_calls')[0]
        assert row['status']=='SKIPPED_PRECHECK' and row['input_tokens']==row['output_tokens']==0
        assert row['llm_call_id'] is None and not row['network_attempted']


@pytest.mark.parametrize('case,expected',[
    ('refusal','PROVIDER_REFUSAL'),('incomplete','INCOMPLETE_FINISH'),('json','JSON_PARSE_ERROR'),
    ('schema','SCHEMA_MISMATCH'),('evidence','EVIDENCE_QUOTE_MISMATCH'),('negation','SEMANTIC_CONFLICT')])
async def test_company_invalid_output_subtypes_still_account_reported_usage(repo,case,expected):
    envelope=response();event=company()
    if case=='refusal':envelope['choices'][0]['message']['refusal']='fixture refusal'
    if case=='incomplete':envelope['choices'][0]['finish_reason']='length'
    if case=='json':envelope['choices'][0]['message']['content']='{invalid'
    if case=='schema':envelope=response(facts(ticker='OTHER'))
    if case=='evidence':
        bad=facts();bad['events'][0]['evidence']='An invented source statement';envelope=response(bad)
    if case=='negation':event=company(body='The company did not report quarterly revenue growth.');
    if case=='negation':
        bad=facts();bad['events'][0]['evidence']='report quarterly revenue growth';envelope=response(bad)
    calls=[]
    def handle(request):calls.append(request);return httpx.Response(200,json=envelope)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        result=await analyzer.analyze(event,'TEST',source_kind='rss',primary_verified=True)
    assert result.validation_error_code==expected and result.failed_field
    assert not result.entry_eligible and result.requires_verification and len(calls)==1
    assert repo.all('llm_calls')[0]['validation_error_code']==expected
    assert repo.all('llm_usage')[0]['input_tokens']==100


@pytest.mark.parametrize('status',[401,403,429,500])
async def test_http_failures_without_usage_never_claim_zero_billed_cost(repo,status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(status,json={'error':{'code':'fixture'}}))) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        result=await analyzer.analyze_macro(macro())
    assert result['error']=='llm_http_'+str(status)
    call=repo.all('llm_calls')[0];usage=repo.all('llm_usage')[0]
    assert call['status']==('RATE_LIMITED' if status==429 else 'HTTP_FAILED')
    assert usage['input_tokens'] is None and usage['cost_status']=='UNCONFIRMED_RESERVED_ESTIMATE'
    assert usage['provider_billed_cost_usd'] is None and Decimal(usage['cost_usd'])>0


async def test_timeout_unknown_outcome_retains_conservative_cost(repo):
    def handle(request):raise httpx.ReadTimeout('fixture transport detail',request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        result=await analyzer.analyze_macro(macro())
    assert result['validation_error_code']=='TIMEOUT'
    assert repo.all('llm_calls')[0]['status']=='OUTCOME_UNKNOWN'
    assert repo.all('llm_usage')[0]['cost_status']=='UNCONFIRMED_RESERVED_ESTIMATE'
    assert 'fixture transport detail' not in json.dumps(repo.all('llm_calls'))


async def test_format_retry_is_explicitly_bounded_and_gets_new_call_id(repo):
    calls=[]
    def handle(request):
        calls.append(request);data=response(macro_facts())
        if len(calls)==1:data['choices'][0]['message']['content']='bad json'
        return httpx.Response(200,json=data)
    settings=LLMSettings(format_retry_limit=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result=await BudgetedAnalyzer(repo,settings,api_key='fixture',client=http,clock=lambda:NOW).analyze_macro(macro())
    assert not result['error'] and len(calls)==2 and len(set(result['llm_call_ids']))==2
    assert [r['status'] for r in repo.all('llm_calls')]==['JSON_PARSE_FAILED','SUCCEEDED']
    assert len(repo.all('llm_usage'))==2 and result['adopted_llm_call_id']==result['llm_call_ids'][-1]


async def test_metadata_write_failure_prevents_http_and_rolls_back_reservation(repo,monkeypatch):
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:calls.append(request))) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        original=repo.record
        def broken(kind,value):
            if kind=='llm_calls':raise RuntimeError('fixture DB unavailable')
            return original(kind,value)
        monkeypatch.setattr(repo,'record',broken)
        result=await analyzer.analyze_macro(macro())
    assert result['error']=='llm_audit_unavailable' and calls==[] and repo.all('llm_usage')==[]
    assert repo.get_control('session_llm_budget_v1',{})=={}


async def test_debug_bodies_are_opt_in_encrypted_masked_and_expire(repo,tmp_path):
    key=tmp_path/'private.key';clock=[NOW]
    options={'enabled':True,'key_file':str(key),'retention_days':7,
             'publishers':{'issuer.test':'https://issuer.test/terms'}}
    event=company(body=BODY+' account_seq=demo-account password=demo-password')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=response()))) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture-key',client=http,clock=lambda:clock[0],audit_settings=options)
        result=await analyzer.analyze(event,'TEST',source_kind='rss',primary_verified=True)
        call_id=result.llm_call_ids[0]
        stored=repo.get_record('llm_audit_bodies',call_id)
        assert stored and BODY not in json.dumps(stored)
        assert analyzer.audit.read_body(call_id) is None
        value=analyzer.audit.read_body(call_id,allow_read=True)
        assert BODY in json.dumps(value) and 'demo-account' not in json.dumps(value) and 'demo-password' not in json.dumps(value)
        assert 'fixture-key' not in json.dumps(value)
        clock[0]+=timedelta(days=8)
        analyzer.audit.cleanup()
        assert repo.all('llm_audit_bodies')==[] and len(repo.all('llm_calls'))==1 and len(repo.all('llm_usage'))==1
    with pytest.raises(ValueError):AuditTrail(repo,options,environment='LIVE_LOCAL')


async def test_same_event_two_analyzers_only_one_network_attempt(repo):
    started=asyncio.Event();release=asyncio.Event();calls=[]
    async def handle(request):
        calls.append(request);started.set();await release.wait();return httpx.Response(200,json=response(macro_facts()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        first=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        second=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        task=asyncio.create_task(first.analyze_macro(macro()))
        await started.wait()
        result=await second.analyze_macro(macro())
        release.set();await task
    assert result['error']=='llm_in_progress' and len(calls)==1 and len(repo.all('llm_usage'))==1


async def test_duplicate_macro_body_across_urls_does_not_create_three_paid_calls(repo):
    calls=[];clock=[NOW]
    def handle(request):calls.append(request);return httpx.Response(200,json=response(macro_facts()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:clock[0])
        original=macro()
        first=await analyzer.analyze_macro(original)
        clock[0]+=timedelta(seconds=1)
        copies=[replace(original,url=f'https://www.bls.gov/copy-{i}') for i in range(2)]
        results=[await analyzer.analyze_macro(value) for value in copies]
    assert first['status']=='CONFIRMED' and len(calls)==len(repo.all('llm_usage'))==1
    assert all(row['error']=='duplicate_article_content' for row in results)
    assert [row['article_id'] for row in results]==[value.id for value in copies]
    assert all(row['duplicate_of_article_id']==original.id for row in results)


async def test_gdelt_verified_body_and_secondary_provenance_trace_to_exact_call(repo):
    from sessionquant.news.enrichment import ArticleEnricher
    from test_stage06_enrichment import FakeFetcher,POLICY,article as discovered
    fetched,value=await ArticleEnricher(repo,policies=POLICY,fetcher=FakeFetcher(),clock=lambda:NOW).enrich(discovered())
    assert value is not None
    data=macro_facts(evidence='announced an interest rate decision')
    calls=[]
    def handle(request):calls.append(request);return httpx.Response(200,json=response(data))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result=await BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW).analyze_macro(value)
    assert result['status']=='UNVERIFIED' and not result['source_verified']
    assert result['publisher_verified'] and not result['primary_official_verified']
    assert result['independent_confirmation_count']==0 and not result['force_liquidation'] and len(calls)==1
    row=repo.all('llm_calls')[0]
    assert row['source_kind']=='gdelt' and row['article_id']==discovered().id
    assert row['enrichment_version']==fetched['parser_version']
    assert row['content_hash']==value.content_hash==repo.all('llm_usage')[0]['content_hash']
    assert row['normalized_result']['evidence_source_span']['paragraph_id']=='p0001'


async def test_gdelt_body_availability_does_not_backdate_to_discovery(repo):
    from sessionquant.news.enrichment import ArticleEnricher
    from test_stage06_enrichment import FakeFetcher,POLICY,article as discovered
    body_at=NOW+timedelta(minutes=2)
    fetched,value=await ArticleEnricher(repo,policies=POLICY,fetcher=FakeFetcher(),clock=lambda:body_at).enrich(discovered())
    assert value.id==discovered().id
    assert fetched['discovered_at']==discovered().received_at.isoformat()
    assert value.first_available_at==value.received_at==body_at
    assert fetched['first_available_at']==body_at.isoformat()
    calls=[]
    def handle(request):calls.append(request);return httpx.Response(200,json=response(macro_facts(evidence='announced an interest rate decision')))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:body_at)
        past=await analyzer.analyze_macro(value,now=NOW+timedelta(minutes=1))
        assert past['status']=='UNKNOWN' and calls==[]
        present=await analyzer.analyze_macro(value,now=body_at)
    assert not present['error'] and len(calls)==1
    assert repo.all('llm_calls')[-1]['first_available_at']==body_at.isoformat()


async def test_legacy_gdelt_flags_without_enrichment_contract_fail_closed(repo):
    value=macro(url='https://publisher.test/news',official=False,discovery_source='gdelt',
        publisher_verified=True,llm_use_allowed=True,document_version='legacy-unproven')
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:calls.append(request))) as http:
        result=await BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW).analyze_macro(value)
    assert result['error']=='enrichment_contract_missing' and calls==[] and repo.all('llm_usage')==[]


async def test_copying_title_into_body_cannot_make_a_paid_candidate(repo):
    calls=[]
    value=macro(body='Employment report')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:calls.append(request))) as http:
        result=await BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW).analyze_macro(value)
    assert result['error']=='title_only_article' and calls==[] and repo.all('llm_usage')==[]


async def test_future_completion_is_not_exposed_or_paid_for_again_at_past_time(repo):
    calls=[]
    def handle(request):calls.append(request);return httpx.Response(200,json=response(macro_facts()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        first=await analyzer.analyze_macro(macro())
        again=await analyzer.analyze_macro(macro())
    assert first['status']=='CONFIRMED' and again['error']=='llm_result_not_yet_available'
    assert len(calls)==1 and len(repo.all('llm_usage'))==1


async def test_company_prompt_change_invalidates_cache_and_keeps_old_audit(repo,monkeypatch):
    import sessionquant.macro.analysis as module
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(200,json=response())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with monkeypatch.context() as patch:
            patch.setattr(module,'COMPANY_PROMPT_VERSION','older-test-prompt')
            old=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
            await old.analyze(company(),'TEST',source_kind='rss',primary_verified=True)
        current=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW+timedelta(seconds=5))
        result=await current.analyze(company(),'TEST',source_kind='rss',primary_verified=True)
        assert not result.cached and len(calls)==2
        rows=repo.recent_llm_attempts()
        assert len(rows)==2 and rows[0]['prompt_version']=='older-test-prompt'
        assert rows[1]['prompt_version']==module.COMPANY_PROMPT_VERSION
