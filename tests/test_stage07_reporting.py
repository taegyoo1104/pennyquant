"""예산·수동 잔액·평가·읽기 전용 상세 화면을 검증한다."""
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.llm.costs import credit_summary,set_manual_credit
from sessionquant.macro.analysis import BudgetedAnalyzer,BudgetLedger,BudgetBlocked
from sessionquant.macro.models import LLMSettings
from sessionquant.macro.evaluation import evaluate_models,classification_metrics
from sessionquant.models import jsonable
from test_stage07_audit import repo,NOW,company,macro,facts,macro_facts,response


async def test_budget_hourly_limit_and_repeated_settlement_are_idempotent(repo):
    settings=LLMSettings(hourly_primary_call_cap=1)
    first=BudgetLedger(repo,settings);second=BudgetLedger(repo,settings)
    reservation=await first.reserve('primary',{'messages':[]},NOW)
    with pytest.raises(BudgetBlocked):await second.reserve('primary',{'messages':[]},NOW)
    await first.settle(reservation,'primary',settings.primary_model,input_tokens=100,output_tokens=20,now=NOW)
    spent=first.snapshot(NOW)['spent_usd']
    await second.settle(reservation,'primary',settings.primary_model,input_tokens=100,output_tokens=20,now=NOW)
    assert second.snapshot(NOW)['spent_usd']==spent and len(repo.all('llm_usage'))==1
    assert second.snapshot(NOW)['reserved_usd']=='0'


async def test_post_response_db_failure_preserves_reservation_and_stops_duplicate_request(repo,monkeypatch):
    calls=[]
    def handle(request):calls.append(request);return httpx.Response(200,json=response(macro_facts()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        analyzer=BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW)
        original=repo.record
        def broken(kind,value):
            if kind=='llm_usage':raise RuntimeError('fixture settlement failure')
            return original(kind,value)
        monkeypatch.setattr(repo,'record',broken)
        result=await analyzer.analyze_macro(macro())
        again=await analyzer.analyze_macro(macro())
    assert result['error']=='llm_audit_unavailable' and again['error']=='llm_in_progress'
    assert len(calls)==1 and repo.all('llm_usage')==[]
    assert Decimal(analyzer.ledger.snapshot(NOW)['reserved_usd'])>0
    assert repo.all('llm_calls')[0]['status']=='REQUESTED'


async def test_failed_escalation_has_its_own_call_and_usage_and_cannot_authorize(repo):
    calls=[]
    negative=facts(overall_sentiment='NEGATIVE')
    negative['events'][0].update(type='DEBT_DEFAULT',shareholder_impact='NEGATIVE',importance='HIGH',
        evidence='failed to pay interest on its debt')
    event=company(body='The company failed to pay interest on its debt under the agreement.')
    def handle(request):
        calls.append(request)
        return httpx.Response(200,json=response(negative)) if len(calls)==1 else httpx.Response(500,json={'error':{'code':'fixture'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result=await BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW).analyze(event,'TEST',source_kind='rss',primary_verified=True)
    assert len(calls)==2 and len(result.llm_call_ids)==2 and result.adopted_llm_call_id is None
    assert not result.entry_eligible and result.requires_verification and result.sentiment=='UNKNOWN'
    assert [row['status'] for row in repo.all('llm_calls')]==['SUCCEEDED','HTTP_FAILED']
    assert {row['stage'] for row in repo.all('llm_usage')}=={'primary','escalation'}


def test_manual_credit_is_not_funding_or_official_real_time_balance(repo):
    repo.set_control('openai_funding_note',{'total_usd':'100'})
    assert credit_summary(repo,NOW)['estimated_credit_usd'] is None
    set_manual_credit(repo,'10',NOW-timedelta(hours=1),now=NOW)
    repo.record('llm_usage',{'id':'old','timestamp':(NOW-timedelta(hours=2)).isoformat(),'cost_usd':'3'})
    repo.record('llm_usage',{'id':'new','timestamp':NOW.isoformat(),'request_started_at':NOW.isoformat(),
        'cost_usd':'0.25','cost_status':'TOKEN_BASED_ESTIMATE'})
    summary=credit_summary(repo,NOW)
    assert summary['estimated_credit_usd']==Decimal('9.75') and summary['manual_balance_usd']==10
    assert summary['provider_balance_verified'] is False and summary['official_organization_cost_usd'] is None
    repo.record('llm_usage',{'id':'crossed','timestamp':NOW.isoformat(),
        'request_started_at':(NOW-timedelta(hours=2)).isoformat(),'cost_usd':'0.5'})
    assert credit_summary(repo,NOW)['estimated_credit_usd'] is None
    with pytest.raises(ValueError):set_manual_credit(repo,'10',NOW+timedelta(seconds=1),now=NOW)


def test_classification_metrics_include_adverse_misses_and_rumor_false_positives():
    rows=[{'expected':'NEGATIVE','actual':'NEUTRAL','expected_status':'CONFIRMED','classification_status':'CONFIRMED','network_attempts':1},
        {'expected':'UNKNOWN','actual':'POSITIVE','expected_status':'RUMOR','classification_status':'CONFIRMED','network_attempts':1},
        {'expected':'NEGATIVE','actual':'NEGATIVE','network_attempts':1}]
    value=classification_metrics(rows)
    assert value['adverse_false_negative']==1 and value['rumor_false_positive']==1
    assert value['per_class']['NEGATIVE']['precision']==1 and value['per_class']['NEGATIVE']['recall']==.5
    assert classification_metrics([])['accuracy'] is None


async def test_synthetic_dataset_batches_preview_without_any_generation(tmp_path,monkeypatch):
    dataset=Path(__file__).resolve().parents[1]/'examples/llm_eval.synthetic.jsonl'
    monkeypatch.setattr(httpx.AsyncClient,'post',lambda *args,**kwargs:pytest.fail('미리보기에서 유료 요청'))
    first=await evaluate_models(dataset,tmp_path/'first.json')
    second=await evaluate_models(dataset,tmp_path/'second.json',offset=20)
    assert first['examples']==20 and second['examples']==18
    assert first['paid_generation_calls']==second['paid_generation_calls']==0
    assert first['results']=={} and first['model_quality_verified'] is False
    with pytest.raises(ValueError):await evaluate_models(dataset,tmp_path/'first.json')


async def test_evaluation_mocked_company_and_macro_report_audit_and_cost(tmp_path):
    dataset=tmp_path/'cases.jsonl'
    rows=[{'id':'company','kind':'company','synthetic':True,'ticker':'TEST','expected_sentiment':'POSITIVE','data':jsonable(company())},
          {'id':'macro','kind':'macro','synthetic':True,'expected_direction':'NEUTRAL','data':{k:v for k,v in jsonable(macro()).items()}}]
    dataset.write_text('\n'.join(json.dumps(row) for row in rows))
    def handle(request):
        schema=json.loads(request.content)['response_format']['json_schema']['name']
        return httpx.Response(200,json=response(facts() if schema=='issuer_news_facts' else macro_facts()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result=await evaluate_models(dataset,tmp_path/'report.json',models=('gpt-4.1-mini',),execute=True,client=http,api_key='fixture-key')
    assert result['paid_generation_calls']==2 and result['model_quality_verified'] is False
    metrics=result['results']['gpt-4.1-mini']
    assert metrics['company']['accuracy']==metrics['macro']['accuracy']==1
    assert metrics['json_parse_failure_rate']==0 and Decimal(metrics['estimated_cost_usd'])>0
    assert (tmp_path/'report.json.audit.db').exists()


async def test_llm_audit_dashboard_is_read_only_and_keeps_legacy_rows(repo,tmp_path,monkeypatch):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=response()))) as http:
        result=await BudgetedAnalyzer(repo,api_key='fixture',client=http,clock=lambda:NOW).analyze(company(),'TEST',source_kind='rss',primary_verified=True)
    repo.record('llm_usage',{'id':'legacy','model':'old-model','input_tokens':2,'cost_usd':'0.01'})
    path=repo.path;calls_before=len(repo.all('llm_calls'))
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite');monkeypatch.setenv('SQ_DB',str(path))
    monkeypatch.setattr('sys.argv',['session_app.py','--db',str(path)])
    monkeypatch.setattr(httpx.AsyncClient,'post',lambda *args,**kwargs:pytest.fail('화면 조회에서 LLM 요청'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'sessionquant/dashboard/session_app.py'),default_timeout=30).run()
    assert not app.exception and not app.error
    app.selectbox(key='llm-call-detail').select(result.llm_call_ids[0]).run()
    assert not app.exception
    values=' '.join(frame.value.to_string() for frame in app.dataframe)
    assert 'SUCCEEDED' in values and 'LEGACY_UNMAPPED' in values
    assert any('원본' in item.value for item in app.caption)
    assert len(repo.all('llm_calls'))==calls_before and repo.all('orders')==[]
