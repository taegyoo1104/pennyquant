"""Paper 가정 격리와 RSS→LLM→후보→가상 체결→청산→재생을 오프라인 검증한다."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import json
import os

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.eligibility import toss_eligibility
from sessionquant.market.history import HistoryStore, KIND, SOURCE
from sessionquant.market.paper_policy import OBSERVED, paper_eligibility
from sessionquant.market.reporting import current_report
from sessionquant.market.quality import quality_summary
from sessionquant.market.universe import UniverseStore
from sessionquant.models import jsonable
from sessionquant.paper_operations import prepare_paper, probe_news, paper_report
from sessionquant.session_config import SessionConfig, load_session_config
from sessionquant.session_demo import synthetic_tape
from sessionquant.session_engine import SessionEngine, event_time
from test_history_selection import prepare
from test_session_market import OPEN, NOW


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name,default in [('SQ_ENVIRONMENT','PAPER_LOCAL'),('SQ_DB_BACKEND','sqlite')]:
        monkeypatch.setenv(name,os.environ.get(name,default))


def listing(stamp=OPEN):
    return toss_eligibility('AAPL',{'symbol':'AAPL','status':'ACTIVE','market':'NASDAQ','currency':'USD'},stamp)


@pytest.fixture
def observed(tmp_path):
    repo=Repository(tmp_path/'observed.db',backend='sqlite')
    store=HistoryStore(repo)
    prepare(store,'AAPL',now=NOW,known=OPEN-timedelta(seconds=1))
    universe=UniverseStore()
    universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=OPEN-timedelta(days=1))
    engine=SessionEngine(repo,universe,synthetic=True,strict_market_data=True,paper_data_policy=OBSERVED,
                         record_inputs=True,candidate_settings={'enabled':True})
    engine.market.history_provider=lambda symbol,stamp:store.load(symbol,stamp,record=True)
    tape,_=synthetic_tape()
    rows=[]
    for row in tape:
        if row['kind']=='rvol':continue
        if row['kind']=='eligibility':row={'kind':'eligibility','data':jsonable(listing())}
        if row['kind']=='bar':row['data'].update(source=SOURCE,price_adjustment='raw')
        if row['kind']=='quote':row['data']['tradable']=False
        rows.append(row)
    yield SimpleNamespace(repo=repo,store=store,engine=engine,rows=rows,universe=universe)
    repo.close()


def enter(value):
    stamp=NOW+timedelta(seconds=1)
    value.engine.apply_batch(sorted([r for r in value.rows if event_time(r)<=stamp],key=event_time),stamp)
    return value.engine.market.evaluate('AAPL',stamp)


def test_observed_uses_calculated_rvol_without_falsifying_account_or_volume_evidence(observed):
    decision=enter(observed)
    assert decision.eligible and decision.signal
    quality=decision.indicators['data_quality']
    assert quality['strategy_rvol']>1 and quality['strategy_rvol_status']=='EXPERIMENTAL_UNVERIFIED'
    assert quality['rvol']['value'] is None and quality['rvol']['status']=='UNKNOWN'
    assert not quality['eligibility_verified']
    assert observed.engine.market.eligibility['AAPL'].fractional is None
    assert not observed.engine.market.quotes['AAPL'].tradable
    intent=observed.repo.all('order_intents')[0]
    assert intent['paper_data_policy']==OBSERVED and intent['paper_assumptions']['actual_account_verified'] is False
    assert intent['strategy_rvol_status']=='EXPERIMENTAL_UNVERIFIED'
    # 상장 참고 자료가 75분 전이어도 Paper 후보가 60초 제한으로 만료되지 않는다.
    from sessionquant.portfolio.selection import VIEW
    frame=observed.repo.get_control(VIEW)
    assert frame['valid_until']>NOW.isoformat() and frame['approved_count']==1


def test_strict_policy_still_blocks_same_inputs(observed):
    observed.engine.market.paper_data_policy='strict'
    decision=enter(observed)
    assert not decision.eligible and observed.repo.all('order_intents')==[]
    assert 'relative_volume_unknown_or_low' in decision.blockers


@pytest.mark.parametrize('field,value', [('tradable',False),('fractional',False),('halted',True),('corporate_action',True)])
def test_explicit_restrictions_are_not_relaxed(field,value):
    result=paper_eligibility(replace(listing(),**{field:value}),NOW)
    assert not result['allowed'] and 'paper_known_restriction_'+field in result['blockers']


@pytest.mark.parametrize('kind', ['missing','stale','future','unknown_exchange','delisted','stale_listing','malformed_listing'])
def test_missing_or_stale_listing_is_not_a_paper_permission(kind):
    item=listing()
    if kind=='missing':item=None
    if kind=='stale':item=listing(OPEN-timedelta(days=2))
    if kind=='future':item=listing(NOW+timedelta(seconds=1))
    if kind=='unknown_exchange':item=replace(item,exchange='UNKNOWN')
    if kind=='delisted':item=replace(item,field_evidence={**item.field_evidence,'listing_status':{
        **item.field_evidence['listing_status'],'value':'DELISTED'}})
    if kind=='stale_listing':item=replace(item,field_evidence={**item.field_evidence,'listing_status':{
        **item.field_evidence['listing_status'],'received_at':(NOW-timedelta(days=2)).isoformat()}})
    if kind=='malformed_listing':item=replace(item,field_evidence={'listing_status':None})
    assert not paper_eligibility(item,NOW)['allowed']


@pytest.mark.parametrize('fault', ['current_gap','history_gap','history_conflict','mixed_source','stale_quote','low_rvol'])
def test_experimental_rvol_does_not_bypass_data_guards(observed,fault):
    if fault=='current_gap':
        observed.rows=[r for r in observed.rows if not (r['kind']=='bar' and r['data']['symbol']=='AAPL'
                       and r['data']['timestamp']==(OPEN+timedelta(minutes=1)).isoformat())]
    if fault in {'history_gap','history_conflict','mixed_source','low_rvol'}:
        history=observed.store.load('AAPL',NOW)
        if fault=='history_gap':history=history[1:]
        if fault=='history_conflict':history=[*history,replace(history[0],volume=history[0].volume+1)]
        if fault=='mixed_source':history=[replace(b,source='OTHER') for b in history]
        if fault=='low_rvol':history=[replace(b,volume=10_000_000) for b in history]
        observed.engine.market.history_provider=lambda *_:history
    if fault=='stale_quote':
        for row in observed.rows:
            if row['kind']=='quote' and row['data']['symbol']=='AAPL':
                row['data'].update(timestamp=(NOW-timedelta(minutes=2)).isoformat(),received_at=(NOW-timedelta(minutes=2)).isoformat())
    decision=enter(observed)
    assert not decision.eligible and observed.repo.all('order_intents')==[]


def test_stale_report_and_restart_policy_mismatch(observed):
    decision=enter(observed)
    q=decision.indicators['data_quality']
    report=current_report(quality_summary([q],NOW),NOW+timedelta(seconds=40))
    assert report['items'][0]['strategy_rvol'] is None
    assert report['items'][0]['paper_eligibility']['allowed'] is False
    with pytest.raises(ValueError):
        SessionEngine(observed.repo,observed.universe,restore=True,synthetic=True,strict_market_data=True)


def test_paper_fill_rechecks_restrictions_and_never_changes_the_observed_quote(observed):
    enter(observed)
    stamp=NOW+timedelta(seconds=2)
    restriction={'kind':'eligibility','data':jsonable(replace(listing(stamp),halted=True))}
    rows=[r for r in observed.rows if event_time(r)==stamp]
    observed.engine.apply_batch([restriction,*rows],stamp)
    assert observed.engine.broker.fills==[]
    assert observed.repo.all('quote_snapshots')[-1]['tradable'] is False


def test_paper_stop_loss_fills_after_entry_even_when_new_entries_are_blocked(observed):
    enter(observed)
    stamp=NOW+timedelta(seconds=2)
    observed.engine.apply_batch([r for r in observed.rows if event_time(r)==stamp],stamp)
    assert [f.side for f in observed.engine.broker.fills]==['buy']
    # 과거 비교 자료를 제거해 신규 진입이 막혀도 손절은 계속한다.
    observed.engine.market.history_provider=lambda *_:[]
    original=observed.engine.market.quotes['AAPL']
    for seconds in (3,4):
        stamp=NOW+timedelta(seconds=seconds)
        quote=replace(original,timestamp=stamp,received_at=stamp,bid=100.,ask=100.02)
        observed.engine.apply_batch([{'kind':'quote','data':jsonable(quote)}],stamp)
    assert [f.side for f in observed.engine.broker.fills]==['buy','sell']
    assert not observed.engine.broker.positions
    assert any(o.intent.reason=='stop_loss' for o in observed.engine.broker.orders.values())


def test_observed_policy_still_waits_for_dashboard_start(observed):
    from sessionquant.trading_controls import begin_session
    observed.engine.control_state=begin_session(observed.repo,mode='paper',now=NOW)
    enter(observed)
    assert observed.repo.all('orders')==[]
    assert observed.repo.get_control('trade_control_state')['status']=='WAITING'


def test_prepare_copies_only_data_with_original_timestamps_and_is_atomic(observed,tmp_path):
    source=SessionConfig(database_backend='sqlite',db_path=observed.repo.path)
    target=replace(source,db_path=tmp_path/'prepared.db',paper_data_policy=OBSERVED)
    observed.repo.record('orders',{'id':'old-trade'})
    observed.repo.set_control('trade_control_state',{'status':'RUNNING'})
    expected=observed.repo.all(KIND)
    report=prepare_paper(source,target)
    assert report['copied_symbol_days']==20 and report['copied_trades']==0
    repo=Repository(target.db_path,backend='sqlite')
    try:
        assert repo.all(KIND)==expected
        assert repo.all('orders')==[] and repo.get_control('trade_control_state') is None
        assert repo.load_state() is None
    finally:repo.close()
    assert observed.repo.all('orders')==[{'id':'old-trade'}]
    with pytest.raises(ValueError,match='이미'):prepare_paper(source,target)
    with pytest.raises(ValueError,match='다른'):prepare_paper(source,replace(source,paper_data_policy=OBSERVED))
    empty=replace(source,db_path=tmp_path/'empty.db')
    destination=replace(target,db_path=tmp_path/'failed.db')
    with pytest.raises(ValueError,match='분봉'):prepare_paper(empty,destination)
    repo=Repository(destination.db_path,backend='sqlite')
    assert not repo.namespace_has_data()
    repo.close()


def test_example_and_empty_report_do_not_claim_trading_success(tmp_path):
    config=load_session_config(Path('config/sessionquant.realtime-paper.example.yaml'))
    assert config.paper_data_policy==OBSERVED and not config.actual_account_monitor_enabled
    repo=Repository(tmp_path/'report.db',backend='sqlite')
    try:
        report=paper_report(repo,config,NOW)
        assert report['engine']['status']=='NOT_STARTED' and not report['engine']['heartbeat_fresh']
        assert report['market']['strategy_rvol_count']==0 and report['namespace_totals']['llm_network_attempts']==0
    finally:repo.close()


def test_prepare_rejects_mysql_namespace_override_aliases_before_opening(tmp_path,monkeypatch):
    monkeypatch.setenv('SQ_DB_NAMESPACE','same-namespace')
    monkeypatch.setenv('SQ_MYSQL_PASSWORD','offline-test-placeholder')
    source=SessionConfig(db_path=tmp_path/'source.db')
    target=replace(source,db_path=tmp_path/'target.db',paper_data_policy=OBSERVED)
    with pytest.raises(ValueError,match='네임스페이스'):prepare_paper(source,target)


def test_cli_prepare_probe_without_key_and_report_file(observed,tmp_path,monkeypatch,capsys):
    from sessionquant.cli import main
    import yaml
    monkeypatch.setenv('OPENAI_API_KEY','')
    src=tmp_path/'source.yaml';dst=tmp_path/'target.yaml'
    source={'database_backend':'sqlite','db_path':str(observed.repo.path),'company_rss_catalog':False}
    src.write_text(yaml.safe_dump(source))
    dst.write_text(yaml.safe_dump({**source,'db_path':str(tmp_path/'prepared.db'),'paper_data_policy':OBSERVED}))
    assert main(['prepare-paper','--source-config',str(src),'--config',str(dst)])==0
    assert json.loads(capsys.readouterr().out)['status']=='PREPARED'
    assert main(['paper-news-probe','--config',str(dst),'--symbol','NVDA'])==0
    assert json.loads(capsys.readouterr().out)['status']=='NO_OPENAI_KEY'
    output=tmp_path/'summary.json'
    assert main(['paper-status','--config',str(dst),'--output',str(output)])==0
    capsys.readouterr()
    report=json.loads(output.read_text())
    assert report['engine']['status']=='NOT_STARTED' and report['last_news_probe']['status']=='NO_OPENAI_KEY'
    assert report['namespace_totals']['llm_network_attempts']==0
    assert main(['paper-status','--config',str(dst),'--output',str(output)])!=0


async def rss_service(repo,monkeypatch,*,valid=True,stale=False):
    from sessionquant.collectors.rss_collector import RSSCollector
    from sessionquant.macro.analysis import BudgetedAnalyzer
    from sessionquant.news.service import NewsService
    from sessionquant.news.schedule import NewsSchedule
    from test_news_service import FakeSec
    from test_stage07_audit import facts,response,BODY
    monkeypatch.setattr('sessionquant.collectors.rss_collector.now_utc',lambda:NOW)
    monkeypatch.setattr('sessionquant.news.analysis.monotonic',lambda:0.)
    published=NOW-timedelta(days=3) if stale else NOW-timedelta(minutes=1)
    xml='<rss version="2.0"><channel><title>Fixture IR</title>'
    for i in range(2):
        xml+=f'<item><guid>{i}</guid><title>Quarterly revenue growth {i}</title><link>https://issuer.test/{i}</link><pubDate>{published.strftime("%a, %d %b %Y %H:%M:%S GMT")}</pubDate><description>{BODY}</description></item>'
    xml+='</channel></rss>'
    requests=[]
    def handle(request):
        requests.append(request)
        if request.method=='GET':return httpx.Response(200,text=xml,headers={'Content-Type':'application/rss+xml'})
        return httpx.Response(200,json=response(facts(ticker='AAPL' if valid else 'OTHER')))
    client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    analyzer=BudgetedAnalyzer(repo,api_key='fixture-only',client=client,clock=lambda:NOW,generation_call_limit=1)
    rss=RSSCollector(client=client,repository=repo,schedule=NewsSchedule(repo,clock=lambda:NOW))
    service=NewsService({'AAPL':{'cik':'1','rss_urls':['https://issuer.test/rss'],'rss_verified':True}},
        analyzer=analyzer,sec_client=FakeSec([]),rss_collector=rss,include_alpaca=False,repository=repo,clock=lambda:NOW)
    return analyzer,service,client,requests


@pytest.mark.parametrize('valid,stale',[(True,False),(False,False),(True,True)])
async def test_rss_probe_uses_real_parsers_and_limits_requests_even_on_invalid_responses(observed,monkeypatch,valid,stale):
    analyzer,service,client,requests=await rss_service(observed.repo,monkeypatch,valid=valid,stale=stale)
    try:
        result=await probe_news(observed.repo,SessionConfig(),'AAPL',analyzer=analyzer,company_service=service,clock=lambda:NOW)
        assert len([r for r in requests if r.method=='POST'])==(0 if stale else 1)
        assert result['llm_generation_calls']==(0 if stale else 1) and result['order_writes']==0
        if valid and not stale:
            assert result['status']=='VALID_RESPONSE_AND_STRATEGY_CONTEXT'
            assert result['strategy_context']['candidate_score_signal']['llm_call_ids']
        else:assert result['status']!='VALID_RESPONSE_AND_STRATEGY_CONTEXT'
        assert observed.repo.all('orders')==[]
    finally:await client.aclose()


async def test_news_to_paper_entry_fill_session_exit_and_exact_recorded_replay(observed,tmp_path,monkeypatch):
    from sessionquant.macro.service import NewsPipeline
    from sessionquant.research import journal
    from sessionquant.research.bundle import export_recording
    from sessionquant.research.replay import replay_recording
    clock=SimpleNamespace(now=NOW)
    monkeypatch.setattr(journal,'clock',lambda:clock.now)
    journal.start(observed.repo,observed.engine,source='toss')
    observed.repo.record('universe_history',observed.universe.snapshots[0])
    analyzer,service,client,requests=await rss_service(observed.repo,monkeypatch)
    pipeline=NewsPipeline(observed.repo,analyzer=analyzer,company_service=service,clock=lambda:clock.now)
    observed.engine.risk_provider=pipeline
    try:
        await pipeline.refresh_company(['AAPL'],NOW)
        clock.now=NOW+timedelta(seconds=1)
        decision=enter(observed)
        assert decision.eligible
        intent=observed.repo.all('order_intents')[0]
        from sessionquant.portfolio.selection import VIEW
        selected=observed.repo.get_control(VIEW)['rows'][0]
        assert selected['news_score_signal']['status']=='VERIFIED'
        assert selected['news_score_signal']['llm_call_ids']
        assert intent['selection_id']==observed.repo.get_control(VIEW)['id']
        for stamp in sorted({event_time(r) for r in observed.rows if event_time(r)>clock.now}):
            clock.now=stamp
            observed.engine.apply_batch([r for r in observed.rows if event_time(r)==stamp],stamp)
        assert [f.side for f in observed.engine.broker.fills]==['buy','sell']
        assert observed.engine.broker.positions=={}
        journal.stop(observed.repo)
        bundle=tmp_path/'bundle'
        export_recording(observed.repo,bundle)
        replay=replay_recording(bundle,tmp_path/'replay.db')
        assert replay['paper_data_policy']==OBSERVED
        assert replay['state']==observed.engine.broker.to_state()
        assert replay['live_order_writes']==0 and replay['llm_api_calls']==0
        assert len([r for r in requests if r.method=='POST'])==1
    finally:
        await pipeline.aclose()
        await client.aclose()
