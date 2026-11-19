"""동시 후보 비교·소프트 목표·예약·위험 우선순위를 격리 Paper로 검증한다."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.engine import MarketEngine
from sessionquant.market.scanner import InstrumentEligibility
from sessionquant.market.universe import UniverseStore
from sessionquant.models import jsonable
from sessionquant.portfolio.broker import PaperBroker
from sessionquant.portfolio.manager import PortfolioManager
from sessionquant.portfolio.models import PortfolioSettings
from sessionquant.portfolio.selection import PortfolioCandidateSelector,CandidateSettings,VIEW
from sessionquant.session_engine import SessionEngine,event_time
from test_session_market import NOW,OPEN,CASES,minutes,quote

SYMBOLS=('AAPL','MSFT','XOM','JPM')


def market_fixture(symbols=SYMBOLS):
    universe=UniverseStore()
    universe.refresh([{'symbol':s,'sector':'Technology' if s in {'AAPL','MSFT'} else 'Energy' if s=='XOM' else 'Financials'}
        for s in symbols],source='SYNTHETIC_UNIVERSE',observed_at=OPEN-timedelta(days=1))
    market=MarketEngine(universe)
    for symbol in ['SPY','QQQ',*symbols]:
        closes=CASES['pullback'] if symbol in symbols else [100+.25*i for i in range(15)]
        for bar in minutes(symbol,closes):market.on_bar(replace(bar,source='SYNTHETIC_QUOTES',price_adjustment='raw'))
        market.on_quote(quote(symbol,closes[-1]))
        market.set_eligibility(symbol,InstrumentEligibility(symbol,NOW,'SYNTHETIC_FIXTURE',True,True,False,False,'NASDAQ'))
        market.set_rvol(symbol,1.5,NOW,verified=True)
    return market


def batch_for(market):
    events=[{'kind':'bar','data':jsonable(bar)} for values in market.bars.values() for bar in values]
    events.extend({'kind':'quote','data':jsonable(item)} for item in market.quotes.values())
    for symbol,item in market.eligibility.items():
        events.append({'kind':'eligibility','data':jsonable(item)})
        events.append({'kind':'rvol','data':{'symbol':symbol,'value':1.5,'observed_at':NOW.isoformat(),'verified':True}})
    return sorted(events,key=event_time)


def session(tmp_path,*,options=None,symbols=SYMBOLS,settings=None,risk_provider=None):
    market=market_fixture(symbols)
    repo=Repository(tmp_path/'selection.db',backend='sqlite')
    engine=SessionEngine(repo,market.universe,synthetic=True,candidate_settings=options or {},settings=settings,risk_provider=risk_provider)
    return repo,engine,market


def test_full_scope_is_ranked_and_only_two_new_symbols_are_reserved(tmp_path):
    repo,engine,market=session(tmp_path)
    engine.apply_batch(batch_for(market),NOW)
    frame=repo.get_control(VIEW)
    assert frame['evaluated_count']==4 and frame['comparison_eligible_count']==4 and frame['final_candidate_count']==2
    assert frame['approved_new_count']==2 and len(engine.broker.get_open_orders())==2
    blocked=[row for row in frame['rows'] if not row['approved']]
    assert all('preferred_position_target_reached' in row['blockers'] for row in blocked)
    intents=repo.all('order_intents')
    assert all(row['selection_id']==frame['id'] for row in intents)
    assert all(row['selection']['order_intent_id'] for row in repo.all('candidates') if row['symbol'] in {item['symbol'] for item in intents})
    assert engine.broker.positions=={} # 승인과 체결은 다르다.
    repo.close()


@pytest.mark.parametrize('profile', ['strategy-lab', 'strategy-lab-websocket'])
def test_lab_allows_three_symbols_counts_pending_buys_and_restores_without_duplicates(tmp_path, profile):
    from sessionquant.session_config import load_session_config
    config = load_session_config('config/sessionquant.'+profile+'.example.yaml')
    settings = PortfolioSettings(**config.portfolio)
    repo, engine, market = session(tmp_path, settings=settings, options=config.candidate_selection)
    try:
        engine.apply_batch(batch_for(market), NOW)
        frame = repo.get_control(VIEW)
        assert frame['approved_new_count'] == 3
        orders = engine.broker.get_open_orders()
        assert len(orders) == 3 and not engine.broker.positions
        fourth = next(row for row in frame['rows'] if not row['approved'])
        assert 'position_limit' in fourth['blockers']
        assert engine.broker.reserved_cash <= settings.initial_cash
        assert sum(o.intent.amount for o in orders) <= settings.initial_cash * 3 * settings.initial_weight
        later = NOW+timedelta(seconds=1)
        engine.apply_batch([{'kind':'quote', 'data':jsonable(replace(market.quotes[o.intent.symbol],
            timestamp=later, received_at=later))} for o in orders[:2]], later)
        assert len(engine.broker.positions) == 2 and len(engine.broker.get_open_orders()) == 1
        restored = SessionEngine(repo, market.universe, settings=settings, synthetic=True,
            candidate_settings=config.candidate_selection, restore=True)
        assert len(restored.broker.positions) == 2 and len(restored.broker.get_open_orders()) == 1
        assert len(repo.all('order_intents')) == 3
        assert restored.broker.reconcile()['ok']
        assert restored.broker.settings.max_positions == restored.broker.settings.preferred_positions == 3
    finally:
        repo.close()


def test_same_time_focus_compares_symbols_without_new_quotes_in_that_batch(tmp_path):
    repo,engine,market=session(tmp_path,symbols=('AAPL','MSFT'))
    engine.market=market
    engine.watchlist_policy=SimpleNamespace(candidate_symbols=lambda now:['AAPL','MSFT'],entry_blockers=lambda *args:[])
    engine.apply_batch([{'kind':'quote','data':jsonable(market.quotes['MSFT'])}],NOW)
    assert {row['symbol'] for row in repo.get_control(VIEW)['rows']}=={'AAPL','MSFT'}
    assert repo.get_control(VIEW)['approved_new_count']==2
    repo.close()


@pytest.mark.parametrize('fault,code',[('quote','candidate_quote_time_mismatch'),('bar','candidate_bar_time_mismatch'),
    ('eligibility','instrument_fractional_unverified_or_restricted'),('news','confirmed_news_entry_restriction')])
def test_high_score_never_bypasses_missing_or_blocked_inputs(tmp_path,fault,code):
    repo,engine,market=session(tmp_path,symbols=('AAPL','MSFT'))
    if fault=='quote':market.quotes['AAPL']=replace(market.quotes['AAPL'],timestamp=NOW-timedelta(seconds=3),received_at=NOW-timedelta(seconds=3))
    if fault=='bar':market.bars['AAPL']=[bar for bar in market.bars['AAPL'] if bar.timestamp<=NOW-timedelta(minutes=5)]
    if fault=='eligibility':market.eligibility['AAPL']=replace(market.eligibility['AAPL'],fractional=None)
    if fault=='news':engine.risk_provider=SimpleNamespace(risk_context=lambda now,symbol:{'block_entries':symbol=='AAPL','major_adverse':False,'blackout':False})
    engine.apply_batch(batch_for(market),NOW)
    row=next(row for row in repo.get_control(VIEW)['rows'] if row['symbol']=='AAPL')
    assert code in row['blockers'] and not row['approved']
    assert all(row['symbol']!='AAPL' for row in repo.all('order_intents'))
    repo.close()


def test_unknown_watchlist_does_not_force_fill(tmp_path):
    repo,engine,market=session(tmp_path)
    engine.watchlist_policy=SimpleNamespace(entry_blockers=lambda *args:['watchlist_unknown'])
    engine.apply_batch(batch_for(market),NOW)
    assert repo.all('order_intents')==[] and repo.get_control(VIEW)['approved_count']==0
    assert all('watchlist_unknown' in row['blockers'] for row in repo.get_control(VIEW)['rows'])
    repo.close()


def test_trading_wait_state_blocks_ranking_approval(tmp_path):
    from sessionquant.trading_controls import begin_session
    repo,engine,market=session(tmp_path)
    engine.control_state=begin_session(repo,mode='paper')
    engine.apply_batch(batch_for(market),NOW)
    assert repo.all('order_intents')==[]
    assert all('trading_not_started_or_halted' in row['blockers'] for row in repo.get_control(VIEW)['rows'])
    repo.close()


def test_selection_expires_with_eligibility_instead_of_quote_only(tmp_path):
    repo,engine,market=session(tmp_path)
    market.eligibility['AAPL']=replace(market.eligibility['AAPL'],valid_until=NOW+timedelta(seconds=1))
    engine.apply_batch(batch_for(market),NOW)
    assert repo.get_control(VIEW)['valid_until']==(NOW+timedelta(seconds=1)).isoformat()
    repo.close()


def test_selection_expiry_includes_watchlist_evidence(tmp_path):
    repo,engine,market=session(tmp_path,symbols=('AAPL','MSFT'))
    engine.watchlist_policy=SimpleNamespace(candidate_symbols=lambda now:['AAPL','MSFT'],entry_blockers=lambda *args:[],
        candidate_expiry=lambda symbol,now:(NOW+timedelta(seconds=2)).isoformat())
    engine.apply_batch(batch_for(market),NOW)
    assert repo.get_control(VIEW)['valid_until']==(NOW+timedelta(seconds=2)).isoformat()
    repo.close()


def test_same_sector_and_pending_buys_are_counted_before_next_entry(tmp_path):
    repo,engine,market=session(tmp_path,options={'max_same_sector':1})
    engine.apply_batch(batch_for(market),NOW)
    frame=repo.get_control(VIEW)
    assert frame['approved_new_count']==2
    selected={row['symbol'] for row in frame['rows'] if row['approved']}
    assert len(selected & {'AAPL','MSFT'})==1
    tech_block=next(row for row in frame['rows'] if row['symbol'] in {'AAPL','MSFT'}-selected)
    assert 'same_sector_limit' in tech_block['blockers']
    # 같은 신호를 다시 평가해도 예약·주문을 중복 생성하지 않는다.
    engine.apply_batch([{'kind':'quote','data':jsonable(quote('AAPL',101.35,NOW+timedelta(seconds=1)))}],NOW+timedelta(seconds=1))
    assert len(repo.all('order_intents'))==2
    repo.close()


def test_diversification_skips_blocked_higher_ranks_before_final_candidate_limit(tmp_path):
    repo,engine,market=session(tmp_path,symbols=('AA','AB','AC','AD','AE','AF','XOM'),options={'max_same_sector':1})
    engine.apply_batch(batch_for(market),NOW)
    frame=repo.get_control(VIEW)
    approved={row['symbol'] for row in frame['rows'] if row['approved']}
    assert frame['evaluated_count']==7 and frame['final_candidate_count']==2
    assert len(approved)==2 and 'XOM' in approved
    assert all('same_sector_limit' in row['blockers'] for row in frame['rows'] if row['symbol'] not in approved)
    repo.close()


def test_diversification_unknown_is_fail_closed_only_when_option_is_active(tmp_path):
    repo,engine,market=session(tmp_path,options={'max_same_sector':1})
    market.universe.refresh([{'symbol':s,'sector':''} for s in SYMBOLS],source='SYNTHETIC_UNKNOWN',observed_at=NOW)
    engine.apply_batch(batch_for(market),NOW)
    assert repo.all('order_intents')==[]
    assert all('sector_diversification_unknown' in row['blockers'] for row in repo.get_control(VIEW)['rows'])
    repo.close()


def test_soft_target_can_be_exceeded_explicitly_but_hard_max_is_unchanged(tmp_path):
    repo,engine,market=session(tmp_path,options={'allow_above_preferred':True})
    engine.apply_batch(batch_for(market),NOW)
    assert repo.get_control(VIEW)['approved_new_count']>=3
    assert len(engine.broker.get_open_orders())<=5
    assert engine.broker.settings.max_positions==5 and engine.broker.settings.preferred_positions==2
    repo.close()


def test_reference_etfs_cannot_be_bought_through_portfolio_manager():
    manager=PortfolioManager(PaperBroker())
    for symbol in ('SPY','QQQ','SHY','IEF','TLT','USO'):
        result=manager.plan_entry(symbol,100,99,NOW,market_state='BULLISH',tradable=True,fractional_supported=True,trend_confirmed=True)
        assert not result.approved and result.reason=='market_reference_not_trading_candidate'


def test_duplicate_strategy_signals_do_not_create_two_candidates_for_one_symbol():
    market=market_fixture(('AAPL','MSFT'));decision=market.evaluate('AAPL',NOW)
    other=replace(decision,strategy='momentum',signal=replace(decision.signal,strategy='momentum'))
    _,frame,_=PortfolioCandidateSelector().rank([(decision,{}),(other,{})],market,PaperBroker(),NOW,synthetic=True)
    assert frame['comparison_eligible_count']==1
    assert sum('duplicate_symbol_signal' in row['blockers'] for row in frame['rows'])==1


def test_restart_keeps_selection_policy_and_order_ids_and_rejects_policy_change(tmp_path):
    repo,engine,market=session(tmp_path)
    engine.apply_batch(batch_for(market),NOW)
    before=[row.client_id for row in engine.broker.get_open_orders()]
    restored=SessionEngine(repo,market.universe,synthetic=True,candidate_settings={},restore=True)
    restored.tick(NOW+timedelta(seconds=1))
    assert [row.client_id for row in restored.broker.get_open_orders()]==before
    with pytest.raises(ValueError,match='후보 선정 정책'):
        SessionEngine(repo,market.universe,synthetic=True,candidate_settings={'use_news_score':False},restore=True)
    repo.close()


@pytest.mark.parametrize('options',[{'final_candidate_limit':6},{'quote_alignment_seconds':True},{'max_same_sector':0},
    {'index_correlation_threshold':float('nan')},{'correlation_minutes':1},{'enabled':'yes'}])
def test_invalid_candidate_policy_is_rejected(options):
    with pytest.raises(ValueError):CandidateSettings(**options)
