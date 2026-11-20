"""선정 가점의 실제 연결·기록 재생·화면 만료·저장 실패 경계를 검사한다."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.database.repository import Repository
from sessionquant.models import jsonable
from sessionquant.macro.service import NewsPipeline
from sessionquant.market_reference.risk import MacroReferenceRiskMonitor
from sessionquant.market_reference.store import VIEW as REFERENCE_VIEW
from sessionquant.portfolio.selection import VIEW
from sessionquant.research import journal
from sessionquant.research.bundle import export_recording
from sessionquant.research.candidate_evaluation import evaluate_candidates
from sessionquant.research.replay import replay_recording
from sessionquant.session_engine import SessionEngine
from sessionquant.dashboard.selection_panel import selection_view
from test_stage08_evidence import positive
from test_stage08_selection import market_fixture,batch_for,NOW
from test_macro_reference_risk import view as reference_view
from test_session_market import quote


def pipeline_with(row):
    pipeline=object.__new__(NewsPipeline)
    pipeline._analyses={row['analysis_id']:row}
    pipeline.economic_events_at=lambda now:[]
    return pipeline


async def test_company_score_changes_priority_and_actual_intents_without_extra_llm_calls(tmp_path,positive):
    market=market_fixture(('AAPL','MSFT','TEST'))
    repo=Repository(tmp_path/'scored.db',backend='sqlite')
    now=NOW+timedelta(seconds=1)
    engine=SessionEngine(repo,market.universe,synthetic=True,candidate_settings={},risk_provider=pipeline_with(positive))
    engine.apply_batch(batch_for(market),now)
    frame=repo.get_control(VIEW)
    assert frame['rows'][0]['symbol']=='TEST' and frame['rows'][0]['news_score_signal']['status']=='VERIFIED'
    assert frame['rows'][0]['approved'] and frame['rows'][0]['order_intent_id']
    assert repo.all('llm_usage')==[] and repo.all('llm_calls')==[]
    assert next(row for row in repo.all('order_intents') if row['symbol']=='TEST')['candidate_rank']==1
    repo.close()


async def test_llm_failure_has_zero_bonus_and_preserves_technical_entries(tmp_path,positive):
    row={**positive,'error':'llm_invalid_output','requires_verification':True}
    market=market_fixture(('AAPL','MSFT','TEST'));repo=Repository(tmp_path/'unknown-news.db',backend='sqlite')
    engine=SessionEngine(repo,market.universe,synthetic=True,candidate_settings={},risk_provider=pipeline_with(row))
    engine.apply_batch(batch_for(market),NOW+timedelta(seconds=1))
    frame=repo.get_control(VIEW)
    assert frame['approved_new_count']==2
    test_row=next(row for row in frame['rows'] if row['symbol']=='TEST')
    assert test_row['indicators']['score_component_status']['news']=='UNKNOWN'
    assert test_row['indicators']['score_components']['news']==0
    repo.close()


@pytest.mark.parametrize('mode',['off','shadow','on','missing'])
def test_macro_filter_is_separate_from_scores_and_missing_on_cannot_be_bypassed(tmp_path,mode):
    market=market_fixture();repo=Repository(tmp_path/'risk.db',backend='sqlite')
    if mode!='missing':repo.set_control(REFERENCE_VIEW,reference_view(risk=True,down=True,at=NOW))
    monitor=MacroReferenceRiskMonitor(repo,{'enabled':mode!='off','paper_shadow_only':mode=='shadow'},research_only=True)
    engine=SessionEngine(repo,market.universe,synthetic=True,candidate_settings={},macro_reference_risk=monitor)
    engine.apply_batch(batch_for(market),NOW)
    frame=repo.get_control(VIEW)
    if mode in {'on','missing'}:
        assert frame['approved_count']==0 and repo.all('order_intents')==[]
        assert all('macro_reference_risk_restriction' in row['blockers'] for row in frame['rows'])
    else:assert frame['approved_new_count']==2
    assert all(row['macro_score_applied'] is False for row in frame['rows'])
    assert frame['macro_reference_risk']['block_entries']==(mode in {'on','missing'})
    repo.close()


def test_daily_loss_has_priority_over_high_candidate_scores(tmp_path):
    market=market_fixture();repo=Repository(tmp_path/'loss.db',backend='sqlite')
    engine=SessionEngine(repo,market.universe,synthetic=True,candidate_settings={})
    engine.broker.roll_session(NOW);engine.broker.cash=Decimal('97')
    engine.apply_batch(batch_for(market),NOW)
    assert repo.all('order_intents')==[]
    assert all(row['allocation_reason']=='daily_loss_limit' for row in repo.get_control(VIEW)['rows'])
    repo.close()


def test_selection_save_failure_rolls_back_intents_and_stops_reuse(tmp_path,monkeypatch):
    market=market_fixture();repo=Repository(tmp_path/'failure.db',backend='sqlite')
    engine=SessionEngine(repo,market.universe,synthetic=True,candidate_settings={})
    original=repo.set_control
    def fail(name,value):
        if name==VIEW:raise RuntimeError('fixture storage failure')
        return original(name,value)
    monkeypatch.setattr(repo,'set_control',fail)
    with pytest.raises(RuntimeError):engine.apply_batch(batch_for(market),NOW)
    assert repo.all('order_intents')==[] and repo.load_state() is None
    with pytest.raises(RuntimeError,match='재시작'):engine.tick(NOW+timedelta(seconds=1))
    repo.close()


async def test_recorded_score_inputs_replay_exactly_and_four_ablations_use_same_bundle(tmp_path,monkeypatch,positive):
    market=market_fixture(('AAPL','MSFT','TEST'))
    repo=Repository(tmp_path/'recorded.db',backend='sqlite');now=NOW+timedelta(seconds=1)
    monkeypatch.setattr(journal,'clock',lambda:now)
    engine=SessionEngine(repo,market.universe,synthetic=True,candidate_settings={},risk_provider=pipeline_with(positive),record_inputs=True)
    journal.start(repo,engine,source='SYNTHETIC_RECORDING')
    repo.record('universe_history',market.universe.snapshots[0])
    engine.apply_batch(batch_for(market),now)
    journal.stop(repo);bundle=tmp_path/'bundle';export_recording(repo,bundle)
    replay=replay_recording(bundle,tmp_path/'replayed.db')
    assert replay['state']==engine.broker.to_state()
    # 내보낸 유니버스의 최초 이용 가능 시각을 보존하므로 선정 ID는 별도로 생성하고 원본 ID를 연결한다.
    financial=lambda rows:[{key:value for key,value in row.items() if key!='selection_id'} for row in rows]
    assert financial(replay['entry_intents'])==financial(repo.all('order_intents'))
    assert replay['portfolio_selections'][0]['source_selection_id']==engine.selection_frame['id']
    second=replay_recording(bundle,tmp_path/'replayed-again.db')
    assert second['entry_intents']==replay['entry_intents']
    report=evaluate_candidates(bundle,tmp_path/'study.json')
    assert set(report['results'])=={'technical_liquidity','company_news','sector_strength','combined'}
    assert report['results']['combined']['verified_news_rows']==1
    assert report['results']['technical_liquidity']['verified_news_rows']==0
    assert report['results']['combined']['order_intent_ids']!=report['results']['technical_liquidity']['order_intent_ids']
    assert not report['actual_out_of_sample_validated'] and report['quality']['synthetic']
    assert report['live_order_writes']==report['llm_generation_calls']==0
    with pytest.raises(ValueError):evaluate_candidates(bundle,tmp_path/'study.json')
    # 원본 장부·원본 선택 결과를 연구가 변경하지 않는다.
    assert repo.load_state()['broker']==engine.broker.to_state()
    repo.close()


def test_news_and_selection_failure_do_not_stop_existing_session_exit(tmp_path):
    from sessionquant.session_demo import synthetic_tape
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_engine import event_time
    rows,opened=synthetic_tape();universe=UniverseStore()
    universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened-timedelta(days=1))
    repo=Repository(tmp_path/'exit.db',backend='sqlite')
    engine=SessionEngine(repo,universe,synthetic=True,candidate_settings={})
    for now in sorted({event_time(row) for row in rows}):
        if engine.broker.positions:
            engine.watchlist_policy=type('NoFreshFocus',(),{'entry_blockers':lambda *args:['focus_unknown_or_stale']})()
        engine.apply_batch([row for row in rows if event_time(row)==now],now)
    assert [row.side for row in engine.broker.fills]==['buy','sell']
    assert engine.broker.positions=={} and any(order.intent.reason=='session_exit' for order in engine.broker.orders.values())
    repo.close()


async def test_watchlist_candidate_expiry_uses_recorded_focus_and_cap_bounds(tmp_path):
    from watchlist_fixtures import context,observe,NOW as WATCHLIST_NOW
    repo,_,_,_,manager=context(tmp_path)
    await manager.refresh(WATCHLIST_NOW)
    now=observe(manager);manager.publish(now)
    symbols=manager.candidate_symbols(now)
    assert symbols
    from sessionquant.models import utc
    for symbol in symbols:
        end=utc(manager.candidate_expiry(symbol,now))
        assert now<=end<=now+timedelta(seconds=manager.config.watchlist_data_max_age_seconds)
    repo.close()


def test_stale_ranking_is_history_and_five_existing_tabs_are_preserved(tmp_path,monkeypatch):
    market=market_fixture();repo=Repository(tmp_path/'dashboard.db',backend='sqlite')
    engine=SessionEngine(repo,market.universe,synthetic=True,candidate_settings={})
    engine.apply_batch(batch_for(market),NOW);frame=repo.get_control(VIEW)
    assert selection_view(frame,NOW+timedelta(seconds=1))['approved_new']==2
    assert selection_view(frame,NOW+timedelta(seconds=30))['approved_new'] is None
    assert not selection_view(frame,NOW-timedelta(seconds=1))['active']
    before=deepcopy(repo.load_state());repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite')
    monkeypatch.setattr('sys.argv',['session_app.py','--db',str(tmp_path/'dashboard.db')])
    app=AppTest.from_file(str(Path(__file__).parents[1]/'sessionquant/dashboard/session_app.py'),default_timeout=30).run()
    assert not app.exception and not app.error
    assert len(app.radio(key='dashboard_page').options)==6
    app.radio(key='dashboard_page').set_value('시장·종목').run()
    app.radio(key='market_detail').set_value('매수 판단').run()
    assert any(metric.label=='현재 최종 후보' and metric.value=='—' for metric in app.metric)
    assert any('마지막 저장된 선정 이력' in caption.value for caption in app.caption)
    repo=Repository(tmp_path/'dashboard.db',backend='sqlite');assert repo.load_state()==before;repo.close()
