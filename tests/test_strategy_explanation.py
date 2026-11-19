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


def test_saved_explanation_is_visible_with_source_time_and_call_id(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from pathlib import Path
    from sessionquant.strategy_lab import demonstrate
    demonstrate(tmp_path/'app')
    path=tmp_path/'app/entry_exit.db'
    repo=Repository(path,backend='sqlite')
    data=inputs()
    repo.set_control(VIEW,{'status':'SUCCEEDED','as_of':NOW.isoformat(),'generated_at':NOW.isoformat(),
        'model':'test-model','llm_call_ids':['test-call'],'facts':data,
        'sections':json.loads(response(data)['choices'][0]['message']['content'])})
    repo.close()
    monkeypatch.setenv('SQ_DB',str(path));monkeypatch.setenv('SQ_DB_BACKEND','sqlite');monkeypatch.setenv('PQ_DB_BACKEND','sqlite')
    app=AppTest.from_file(str(Path('sessionquant/dashboard/session_app.py').resolve()),default_timeout=30).run()
    assert not app.exception
    assert any('AI 설명 · 시장' in e.label for e in app.expander)
    assert any('test-call' in c.value for c in app.caption)
