import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.macro.analysis import BudgetedAnalyzer
from sessionquant.strategy_explanation import StrategyExplainer, snapshot, VIEW

NOW=datetime(2026,11,13,16,tzinfo=timezone.utc)


def inputs():
    engine=SimpleNamespace(selection_frame={'rows':[{'symbol':'AAPL','evaluated_at':NOW.isoformat(),
        'indicators':{'strategy_pattern':{'checks':[{'key':'rebound','label':'반등','passed':False}]}},
        'blockers':['simple_rebound_missing'],'portfolio_status':'BLOCKED'}]},
        strategy='pullback_simple',synthetic=True,broker=SimpleNamespace(fills=[]))
    return snapshot(engine,None,NOW)


def response(data):
    result={key:{'text':'입력 기록에 따른 설명입니다.','evidence_ids':[data[key][0]['id']]}
            for key in ('market','strategy','trades')}
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}],
            'usage':{'prompt_tokens':300,'completion_tokens':100}}


async def test_explanation_uses_audited_budgeted_transport_and_survives_restart(tmp_path):
    data=inputs();calls=[];clock=[NOW]
    async def request(req):
        calls.append(req)
        assert 'strategy_snapshot' not in req.url.path
        assert 'NO_RECENT_ANALYZED_NEWS' in req.content.decode()
        return httpx.Response(200,json=response(data))
    repo=Repository(tmp_path/'ai.db',backend='sqlite')
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        analyzer=BudgetedAnalyzer(repo,api_key='test',client=client,clock=lambda:clock[0])
        config={'enabled':True,'min_interval_seconds':60,'max_attempts_per_day':1}
        service=StrategyExplainer(repo,analyzer,config)
        result=await service.refresh(data,NOW)
        assert result['status']=='SUCCEEDED' and len(result['llm_call_ids'])==1
        assert result['role']=='EXPLANATION_ONLY' and result['synthetic_inputs']
        assert repo.recent('llm_calls',1)[0]['status']=='SUCCEEDED'
        assert repo.recent('llm_usage',1) and not repo.all('orders')
        clock[0]=NOW+timedelta(minutes=2)
        restarted=StrategyExplainer(repo,analyzer,config)
        changed=deepcopy(data);changed['as_of']=clock[0].isoformat()
        changed['strategy'][0]['evaluated_at']=clock[0].isoformat()
        assert await restarted.refresh(changed,clock[0]) is None
        changed['strategy'][0]['blockers']=['another_condition']
        assert (await restarted.refresh(changed,clock[0]))['status']=='DAILY_LIMIT'
        assert len(calls)==1
    repo.close()


@pytest.mark.parametrize('fault',['unknown_evidence','order_instruction','truncated','http_error','budget','no_key'])
async def test_explanation_failure_never_creates_order_or_fake_success(tmp_path,fault):
    data=inputs();payload=response(data);calls=[]
    parsed=json.loads(payload['choices'][0]['message']['content'])
    if fault=='unknown_evidence':parsed['strategy']['evidence_ids']=['invented']
    if fault=='order_instruction':parsed['place_order']={'symbol':'AAPL','qty':100}
    payload['choices'][0]['message']['content']=json.dumps(parsed)
    if fault=='truncated':payload['choices'][0]['finish_reason']='length'
    def handler(req):
        calls.append(req)
        return httpx.Response(503 if fault=='http_error' else 200,json=payload)
    repo=Repository(tmp_path/'failure.db',backend='sqlite')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer=BudgetedAnalyzer(repo,api_key='' if fault=='no_key' else 'test',client=client,clock=lambda:NOW)
        if fault=='budget':
            from sessionquant.macro.budget import BudgetBlocked
            async def blocked(*args,**kwargs):raise BudgetBlocked('test cap')
            analyzer.ledger.reserve=blocked
        result=await StrategyExplainer(repo,analyzer,{'enabled':True}).refresh(data,NOW)
        assert result['status']!='SUCCEEDED' and 'sections' not in result
        assert not repo.all('orders') and not repo.all('executions')
        if fault in {'budget','no_key'}:assert not calls
    repo.close()


async def test_slow_model_does_not_block_engine_order_and_fill(tmp_path):
    from sessionquant.session_demo import synthetic_tape
    from sessionquant.session_engine import SessionEngine,event_time
    from sessionquant.market.universe import UniverseStore
    started=asyncio.Event();release=asyncio.Event();data=inputs()
    async def handler(req):
        started.set();await release.wait()
        return httpx.Response(200,json=response(data))
    repo=Repository(tmp_path/'slow.db',backend='sqlite')
    tape,opened=synthetic_tape()
    universe=UniverseStore();universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened-timedelta(days=1))
    engine=SessionEngine(repo,universe,strategy='pullback_simple',synthetic=True,use_market_filter=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer=BudgetedAnalyzer(repo,api_key='test',client=client,clock=lambda:NOW)
        task=asyncio.create_task(StrategyExplainer(repo,analyzer,{'enabled':True}).refresh(data,NOW))
        try:
            await asyncio.wait_for(started.wait(),2)
            for stamp in sorted({event_time(r) for r in tape}):
                engine.apply_batch([r for r in tape if event_time(r)==stamp and r['kind']!='rvol'],stamp)
            assert [f.side for f in engine.broker.fills]==['buy','sell']
            assert not task.done()
        finally:
            release.set();await task
    repo.close()


@pytest.mark.parametrize('has_decision', [True, False])
def test_saved_explanation_is_visible_with_source_time_and_call_id(tmp_path,monkeypatch,has_decision):
    from streamlit.testing.v1 import AppTest
    from pathlib import Path
    from sessionquant.strategy_lab import demonstrate
    demonstrate(tmp_path/'app')
    path=tmp_path/'app/entry_exit.db'
    repo=Repository(path,backend='sqlite')
    data=inputs()
    if not has_decision:
        data['strategy']=[{'id':'strategy:none','status':'NO_RECORDED_DECISION'}]
    sections=json.loads(response(data)['choices'][0]['message']['content'])
    sections['strategy']['text']=("현재 전략 'pullback_simple'의 반등 조건을 확인 중입니다." if has_decision
        else "현재 전략 'pullback_simple'은 기록된 결정이 없고 부정적 뉴스 때문에 매매가 막혔습니다.")
    repo.set_control(VIEW,{'status':'SUCCEEDED','as_of':NOW.isoformat(),'generated_at':NOW.isoformat(),
        'model':'test-model','llm_call_ids':['test-call'],'facts':data,
        'sections':sections})
    repo.close()
    monkeypatch.setenv('SQ_DB',str(path));monkeypatch.setenv('SQ_DB_BACKEND','sqlite');monkeypatch.setenv('PQ_DB_BACKEND','sqlite')
    app=AppTest.from_file(str(Path('sessionquant/dashboard/session_app.py').resolve()),default_timeout=30).run()
    assert not app.exception
    assert any('AI 브리핑' in e.value and '입력 기준' in e.value for e in app.markdown)
    summary=next(e.value for e in app.markdown if '<h3>AI 브리핑</h3>' in e.value)
    expected='단순 눌림목' if has_decision else '기록된 전략 판단이 없습니다'
    assert expected in summary and 'pullback_simple' not in summary
    assert '부정적 뉴스 때문에' not in summary
    app.radio(key='dashboard_page').set_value('뉴스·LLM').run()
    assert not app.exception
    assert any('AI 설명 · 시장' in e.label for e in app.expander)
    assert any('test-call' in c.value for c in app.caption)
    assert any(expected in text.value for text in app.text)
    assert not any('pullback_simple' in text.value for text in app.text)
    repo=Repository(path,backend='sqlite')
    assert repo.get_control(VIEW)['sections']==sections
    repo.close()


async def test_saved_success_survives_limits_restart_and_next_success(tmp_path):
    from sessionquant.macro.budget import BudgetBlocked
    from sessionquant.dashboard.explanation_panel import saved_explanation, explanation_status
    repo=Repository(tmp_path/'retained.db',backend='sqlite')
    data=inputs();clock=[NOW];fault=[None]
    async def handler(req):
        return httpx.Response(200,json=response(data))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer=BudgetedAnalyzer(repo,api_key='test',client=client,clock=lambda:clock[0])
        reserve=analyzer.ledger.reserve
        async def maybe_block(*args,**kwargs):
            if fault[0]:raise BudgetBlocked('secret-should-not-be-persisted',code=fault[0])
            return await reserve(*args,**kwargs)
        analyzer.ledger.reserve=maybe_block
        config={'enabled':True,'min_interval_seconds':60,'max_attempts_per_day':10}
        first=deepcopy(await StrategyExplainer(repo,analyzer,config).refresh(data,NOW))
        for code in ('HOURLY_CALL_CAP','DAILY_CALL_CAP','MONTHLY_COST_CAP','PROVIDER_COOLDOWN'):
            clock[0]+=timedelta(minutes=2);fault[0]=code
            data['strategy'][0]['blockers']=[code];data['as_of']=clock[0].isoformat()
            result=await StrategyExplainer(repo,analyzer,config).refresh(data,clock[0])
            assert result['status']=='BUDGET_BLOCKED' and result['limit_reason']==code
            assert saved_explanation(result)==first
            assert result['as_of']!=first['as_of'] and 'last_success' not in result['last_success']
            assert 'secret-should-not-be-persisted' not in json.dumps(result)
            assert explanation_status(result)!=explanation_status({'status':'BUDGET_BLOCKED'})
        clock[0]+=timedelta(minutes=2);fault[0]=None
        final=await StrategyExplainer(repo,analyzer,config).refresh(data,clock[0])
        assert final['status']=='SUCCEEDED' and 'last_success' not in final
        assert repo.get_control(VIEW)==final
    assert not repo.all('orders') and not repo.all('executions')
    repo.close()


async def test_session_only_explanation_preserves_budget_until_regular_open(tmp_path):
    repo=Repository(tmp_path/'hours.db',backend='sqlite');calls=[];clock=[NOW.replace(hour=13)]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json=response(inputs()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer=BudgetedAnalyzer(repo,api_key='test',client=client,clock=lambda:clock[0])
        service=StrategyExplainer(repo,analyzer,{'enabled':True,'regular_session_only':True})
        for at in (NOW.replace(hour=13),NOW.replace(hour=21),NOW+timedelta(days=1)):
            assert await service.refresh(inputs(),at) is None
        assert not calls and not service.state
        clock[0]=NOW.replace(hour=14,minute=30)
        assert (await service.refresh(inputs(),clock[0]))['status']=='SUCCEEDED'
        assert len(calls)==1
    repo.close()
