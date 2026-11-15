"""검증된 기업 사실과 동일 구간 업종 자료만 가점에 기여하는지 검사한다."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.macro.analysis import BudgetedAnalyzer
from sessionquant.news.ranking import company_score,score_value
from sessionquant.portfolio.strength import RelativeStrength
from sessionquant.portfolio.selection import PortfolioCandidateSelector
from sessionquant.portfolio.broker import PaperBroker
from test_stage07_audit import company,response,NOW as NEWS_NOW
from test_stage08_selection import market_fixture,NOW


@pytest.fixture
async def positive(tmp_path):
    repo=Repository(tmp_path/'news.db',backend='sqlite')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=response()))) as client:
        analyzer=BudgetedAnalyzer(repo,api_key='dummy',client=client,clock=lambda:NOW)
        result=await analyzer.analyze(company(published_at=NOW-timedelta(minutes=1),received_at=NOW),'TEST',source_kind='rss',primary_verified=True)
    repo.close()
    return result.to_dict()|{'category':'company'}


async def test_verified_company_facts_are_scored_but_macro_explanations_are_not(positive):
    score=company_score([positive],'TEST',NEWS_NOW+timedelta(seconds=1))
    assert score['status']=='VERIFIED' and score['value']==1 and score['analysis_ids']==[positive['analysis_id']]
    assert score_value(score,'TEST',NEWS_NOW+timedelta(seconds=1))==1
    macro={**positive,'category':'macro','direction':'POSITIVE','source_verified':True,'importance':'HIGH'}
    assert company_score([macro],'TEST',NEWS_NOW+timedelta(seconds=1))['value'] is None
    assert score['macro_score_applied'] is False


@pytest.mark.parametrize('fault',['rumor','unverified','error','stale','future','quote','ticker','missing','neutral'])
async def test_bad_or_missing_evidence_has_no_positive_bonus(positive,fault):
    row=deepcopy(positive);now=NEWS_NOW+timedelta(seconds=1)
    if fault=='rumor':row['events'][0]['status']='RUMOR'
    if fault=='unverified':row['primary_verified']=False
    if fault=='error':row['error']='llm_invalid_output'
    if fault=='stale':now+=timedelta(days=2)
    if fault=='future':row['analyzed_at']=(now+timedelta(seconds=1)).isoformat()
    if fault=='quote':row['events'][0]['evidence']='Invented evidence that never appeared.'
    if fault=='ticker':row['event']['symbols']=['OTHER']
    if fault=='missing':row.pop('evidence_source_spans')
    if fault=='neutral':row['sentiment']='NEUTRAL';row['events'][0]['shareholder_impact']='NEUTRAL'
    score=company_score([row],'TEST',now)
    if fault=='neutral':assert score['status']=='VERIFIED' and score['value']==.5
    else:assert score['status']=='UNKNOWN' and score['value'] is None


async def test_later_failed_revision_suppresses_old_positive_instead_of_reusing_it(positive):
    later={**positive,'error':'llm_invalid_output','analyzed_at':(NEWS_NOW+timedelta(seconds=1)).isoformat()}
    assert company_score([positive,later],'TEST',NEWS_NOW+timedelta(seconds=2))['value'] is None


def test_complete_known_sector_uses_same_window_and_unknown_is_not_neutral():
    market=market_fixture()
    report=RelativeStrength(market,NOW,synthetic=True).sector_strength('AAPL')
    assert report['status']=='VERIFIED' and report['required_members']==['AAPL','MSFT']
    assert report['input_hash'] and report['membership_id'] and report['end_at']==NOW.isoformat()
    market.bars['MSFT'].pop()
    missing=RelativeStrength(market,NOW,synthetic=True).sector_strength('AAPL')
    assert missing['status']=='UNKNOWN' and missing['value'] is None


def test_null_sector_membership_is_unknown_and_does_not_break_candidate_selection():
    market=market_fixture()
    market.universe.refresh([{'symbol':s,'sector':None} for s in ('AAPL','MSFT')],source='SYNTHETIC_INCOMPLETE',observed_at=NOW)
    strength=RelativeStrength(market,NOW,synthetic=True)
    assert strength.sector('AAPL') is None and strength.sector_strength('AAPL')['status']=='UNKNOWN'
    _,frame,_=PortfolioCandidateSelector().rank([(market.evaluate('AAPL',NOW),{})],market,PaperBroker(),NOW,synthetic=True)
    assert frame['rows'][0]['indicators']['score_component_status']['sector']=='UNKNOWN'


def test_no_operating_price_comparability_or_future_receipt_means_unknown_sector():
    market=market_fixture()
    assert RelativeStrength(market,NOW).sector_strength('AAPL')['status']=='UNKNOWN'
    market.bars['MSFT'][-1]=replace(market.bars['MSFT'][-1],received_at=NOW+timedelta(seconds=1))
    assert RelativeStrength(market,NOW,synthetic=True).sector_strength('AAPL')['status']=='UNKNOWN'


def test_comparable_provider_series_supports_operating_sector_calculation():
    market=market_fixture()
    for symbol in market.bars:
        market.bars[symbol]=[replace(bar,source='fixture-provider:bars') for bar in market.bars[symbol]]
        proof={'status':'VERIFIED','source':'fixture-provider:bars','split_evidence_source':'fixture:corporate-actions',
            'received_at':NOW.isoformat(),'valid_until':(NOW+timedelta(minutes=1)).isoformat(),
            'range_start':market.bars[symbol][0].timestamp.isoformat(),'range_end':NOW.isoformat(),
            'price_adjustment':'raw','volume_policy':'no_splits_verified'}
        market.eligibility[symbol]=replace(market.eligibility[symbol],volume_evidence=proof)
    value=RelativeStrength(market,NOW).sector_strength('AAPL')
    assert value['status']=='VERIFIED' and value['source']=='fixture-provider:bars' and value['synthetic'] is False
    market.eligibility['MSFT'].volume_evidence['valid_until']=(NOW+timedelta(seconds=1)).isoformat()
    assert RelativeStrength(market,NOW).sector_strength('AAPL')['valid_until']==(NOW+timedelta(seconds=1)).isoformat()
    market.bars['MSFT'][-1]=replace(market.bars['MSFT'][-1],price_adjustment='adjusted')
    assert RelativeStrength(market,NOW).sector_strength('AAPL')['status']=='UNKNOWN'


def test_unrelated_oil_etf_changes_do_not_add_energy_stock_points():
    market=market_fixture();selector=PortfolioCandidateSelector()
    decision=market.evaluate('XOM',NOW)
    _,before,_=selector.rank([(decision,{})],market,PaperBroker(),NOW,synthetic=True)
    market.bars['USO']=[replace(bar,symbol='USO',close=bar.close*1.02,high=bar.high*1.03) for bar in market.bars['SPY']]
    _,after,_=selector.rank([(decision,{'macro_context_llm':{'direction':'POSITIVE'}})],market,PaperBroker(),NOW,synthetic=True)
    assert before['rows'][0]['candidate_score']==after['rows'][0]['candidate_score']


def test_index_correlation_constraint_counts_already_reserved_exposure():
    market=market_fixture()
    for symbol in ('AAPL','MSFT'):
        market.bars[symbol]=[replace(bar,symbol=symbol) for bar in market.bars['SPY']]
    strength=RelativeStrength(market,NOW,synthetic=True)
    assert strength.index_correlation('MSFT')['value']>.99
    broker=type('FixtureBroker',(),{})()
    broker.positions={'AAPL':object()};broker.get_open_orders=lambda:[];broker.exit_intents={}
    broker.settings=PaperBroker().settings
    selector=PortfolioCandidateSelector({'max_high_index_correlation':1})
    assert selector.admission_blockers({'symbol':'MSFT'},broker,strength)==['high_index_correlation_limit']
