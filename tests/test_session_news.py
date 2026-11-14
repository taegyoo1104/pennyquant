"""뉴스의 출처·시각·예산·독립 처리와 청산 작업 격리를 검증한다."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import asyncio

import pytest

from sessionquant.database.repository import Repository
from sessionquant.macro.analysis import BudgetBlocked, BudgetLedger, BudgetedAnalyzer
from sessionquant.macro.models import LLMSettings, MacroArticle, EconomicEvent
from sessionquant.macro.service import NewsPipeline

NOW = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)

@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path/'news.db', backend='sqlite')
    yield r
    r.close()

async def test_budget_reservations_survive_restart_and_daily_limit(repo):
    settings = LLMSettings(daily_primary_call_cap=1)
    ledger = BudgetLedger(repo, settings)
    payload = {'messages':[{'role':'user','content':'검증할 기사'}]}
    reservation = await ledger.reserve('primary', payload, NOW)
    second = BudgetLedger(repo, settings)
    assert Decimal(second.snapshot(NOW)['reserved_usd']) > 0
    with pytest.raises(BudgetBlocked):
        await second.reserve('primary', payload, NOW)
    await second.settle(reservation, 'primary', settings.primary_model, input_tokens=1000, output_tokens=200, now=NOW)
    usage = repo.all('llm_usage')[0]
    assert usage['input_tokens'] == 1000 and usage['cost_is_estimate'] is False
    assert Decimal(second.snapshot(NOW)['reserved_usd']) == 0
    assert Decimal(usage['cost_usd']) == Decimal('0.00072')

async def test_monthly_hard_cap_applies_before_request(repo):
    settings = LLMSettings(monthly_soft_budget_usd='0', monthly_hard_budget_usd='0')
    ledger = BudgetLedger(repo, settings)
    with pytest.raises(BudgetBlocked):
        await ledger.reserve('escalation', {'messages':[]}, NOW)
    assert repo.all('llm_usage') == []

async def test_missing_key_never_fabricates_confirmed_macro_event(repo):
    analyzer = BudgetedAnalyzer(repo, api_key='', clock=lambda: NOW)
    article = MacroArticle('https://example.com/war', 'War rumor may affect oil', '', 'GDELT', None, NOW,
                           discovery_source='gdelt')
    result = await analyzer.analyze_macro(article, now=NOW)
    assert result['status'] != 'CONFIRMED'
    assert result['force_liquidation'] is False
    assert repo.all('llm_usage') == []
    await analyzer.aclose()


def risk_pipeline(repo):
    pipeline = object.__new__(NewsPipeline)
    pipeline.repo = repo
    pipeline._analyses, pipeline._economic = {}, {}
    return pipeline


def test_news_risk_requires_provenance_completion_and_independent_event(repo):
    p = risk_pipeline(repo)
    row = {'id':'negative','category':'company','ticker':'AAPL','published_at':(NOW-timedelta(minutes=2)).isoformat(),
           'analyzed_at':NOW.isoformat(),'primary_verified':False,'requires_verification':False,
           'confidence':.95,'missing_information':[],'error':'','reason':'중요 공시',
           'events':[{'status':'CONFIRMED','importance':'HIGH','shareholder_impact':'NEGATIVE'}]}
    p._analyses['negative'] = row
    assert p.risk_context(NOW, 'AAPL')['major_adverse'] is False
    row['primary_verified'] = True
    assert p.risk_context(NOW, 'AAPL')['major_adverse'] is True
    assert p.risk_context(NOW-timedelta(seconds=1), 'AAPL')['major_adverse'] is False
    assert p.risk_context(NOW, 'MSFT')['block_entries'] is False
    p._analyses['independent_positive'] = {**row,'id':'positive','events':[], 'reason':'다른 사건'}
    assert p.risk_context(NOW, 'AAPL')['major_adverse'] is True


def test_verified_calendar_blocks_only_known_announcement_window(repo):
    p = risk_pipeline(repo)
    event = EconomicEvent('cpi', 'CPI', NOW, NOW-timedelta(days=1),'https://www.bls.gov/news.release/cpi.htm',verified=True)
    p.add_economic_event(event)
    assert p.risk_context(NOW)['blackout'] is True
    assert p.risk_context(NOW+timedelta(minutes=16))['blackout'] is False
    with pytest.raises(ValueError):
        p.add_economic_event(EconomicEvent('bad', '발표', NOW, NOW,'https://example.com',verified=True))

async def test_slow_news_task_does_not_block_market_tick_or_shutdown(tmp_path, monkeypatch):
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_config import SessionConfig
    from sessionquant.session_runtime import run_session
    now = datetime.now(timezone.utc)
    universe = tmp_path/'universe.json'
    UniverseStore(universe).refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=now)
    source = tmp_path/'feed.jsonl'
    source.touch()
    stopped = asyncio.Event()
    class SlowNews:
        def __init__(self,*args,**kwargs): pass
        async def refresh(self,*args,**kwargs):
            try: await asyncio.Event().wait()
            finally: stopped.set()
        def risk_context(self,*args,**kwargs): return {'status':'UNKNOWN','block_entries':False,'major_adverse':False}
        async def aclose(self): pass
    monkeypatch.setattr('sessionquant.macro.service.NewsPipeline',SlowNews)
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite')
    monkeypatch.setenv('SQ_ENVIRONMENT','PAPER_LOCAL')
    config=SessionConfig(database_backend='sqlite',db_path=tmp_path/'paper.db',universe_path=universe,
                         source='file',input_path=source)
    await asyncio.wait_for(run_session(config,no_dashboard=True,max_cycles=2,desktop_notifications=False),timeout=12)
    assert stopped.is_set()
    r=Repository(config.db_path,backend='sqlite')
    assert r.get_control('sessionquant_context')['live_orders_enabled'] is False
    r.close()

async def test_treasury_html_preserves_unknown_publication_time():
    import httpx
    from sessionquant.macro.sources import TreasuryClient
    client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,text='''
        <a href="/news/press-releases/jy1234">Treasury announces new financial policy</a>
        <a href="https://example.com/news/press-releases/fake">External false official release</a>''')))
    source=TreasuryClient(client=client)
    articles=await source.fetch()
    assert len(articles)==1 and articles[0].published_at is None
    assert articles[0].official is True
    await client.aclose()

async def test_model_evaluation_defaults_to_no_paid_calls(tmp_path,monkeypatch):
    from sessionquant.macro.evaluation import evaluate_models
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    import json
    file=tmp_path/'evaluation.jsonl'
    file.write_text(json.dumps({'ticker':'AAPL','expected_sentiment':'UNKNOWN','data':{
        'id':'fixture','symbols':['AAPL'],'title':'Unconfirmed rumor','body':'The company may act.',
        'source':'fixture','published_at':NOW.isoformat(),'received_at':NOW.isoformat()}}))
    result=await evaluate_models(file,tmp_path/'report.json')
    assert result['execute'] is False and not result['results']
    with pytest.raises(ValueError,match='OPENAI_API_KEY'):
        await evaluate_models(file,tmp_path/'paid.json',execute=True)
