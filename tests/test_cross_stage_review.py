"""00~08 종합 검토에서 재현한 시계 역전·청구 혼동의 회귀를 막는다."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.universe import UniverseStore
from sessionquant.models import Quote
from sessionquant.portfolio.broker import PaperBroker
from sessionquant.research.costs import summarize_llm_costs
from sessionquant.session_engine import SessionEngine

NOW=datetime(2026,11,13,16,tzinfo=timezone.utc)


def test_backward_session_day_cannot_reset_daily_loss_latch():
    broker=PaperBroker();broker.roll_session(NOW);broker.cash=Decimal('97')
    assert broker.check_daily_loss(NOW)
    before=broker.to_state()
    with pytest.raises(ValueError,match='과거 거래일'):broker.roll_session(NOW-timedelta(days=1))
    assert broker.to_state()==before and broker.check_daily_loss(NOW)


def test_empty_ticks_enforce_decision_clock_across_restart_and_keep_older_receipts_usable(tmp_path):
    repo=Repository(tmp_path/'clock.db',backend='sqlite');universe=UniverseStore()
    engine=SessionEngine(repo,universe,synthetic=True)
    engine.tick(NOW);before=deepcopy(repo.load_state())
    with pytest.raises(ValueError,match='전략 처리 시각'):engine.tick(NOW-timedelta(seconds=1))
    assert repo.load_state()==before and not engine.failed
    restored=SessionEngine(repo,universe,synthetic=True,restore=True)
    with pytest.raises(ValueError):restored.tick(NOW-timedelta(days=1))
    # 처리 시각과 실제 수신 시각을 혼동하지 않는다. 직전 tick 이전에 수신한 자료도 현재 now로 처리할 수 있다.
    quote=Quote('SPY',NOW-timedelta(seconds=1),NOW-timedelta(seconds=1),99,100,100,100)
    from sessionquant.models import jsonable
    restored.apply_batch([{'kind':'quote','data':jsonable(quote)}],NOW+timedelta(seconds=1))
    assert restored.market.quotes['SPY'].received_at==quote.received_at
    assert repo.load_state()['last_decision_at']==(NOW+timedelta(seconds=1)).isoformat()
    repo.close()


def test_legacy_engine_state_uses_existing_broker_clock_without_resetting_ids(tmp_path):
    repo=Repository(tmp_path/'legacy-clock.db',backend='sqlite');universe=UniverseStore()
    engine=SessionEngine(repo,universe,synthetic=True);engine.tick(NOW)
    stored=repo.load_state();stored.pop('last_decision_at');repo.save_state(stored)
    restored=SessionEngine(repo,universe,synthetic=True,restore=True)
    assert restored.last_decision_at==NOW
    with pytest.raises(ValueError):restored.tick(NOW-timedelta(seconds=1))
    assert repo.all('orders')==[]
    repo.close()


def test_measured_tokens_and_verified_prices_are_still_estimates_not_verified_bills():
    row={'id':'call-1','cost_usd':'0.20','cost_status':'TOKEN_BASED_ESTIMATE',
        'pricing_verified':True,'cost_is_estimate':False,'provider_billed_cost_usd':None}
    result=summarize_llm_costs([row])
    assert result['llm_recorded_estimated_usd']==Decimal('.20')
    assert result['llm_recorded_verified_usd'] is None and not result['llm_billing_verified']


def test_usage_revisions_are_not_counted_as_new_calls_and_unknown_cost_stays_unknown():
    first={'id':'call-1','llm_call_id':'call-1','cost_usd':'.20','cost_status':'UNCONFIRMED_RESERVED_ESTIMATE'}
    settled={**first,'cost_usd':'.05','cost_status':'TOKEN_BASED_ESTIMATE'}
    result=summarize_llm_costs([first,settled])
    assert result['llm_recorded_estimated_usd']==Decimal('.05')
    assert result['llm_usage_records']==1 and result['llm_superseded_usage_versions']==1
    assert summarize_llm_costs([{'id':'legacy'}])['llm_recorded_estimated_usd'] is None
    assert summarize_llm_costs([])['llm_recorded_verified_usd'] is None


def test_only_explicit_billing_reconciliation_can_populate_verified_cost():
    row={'id':'call-1','cost_usd':'.05','cost_status':'PROVIDER_BILLED_VERIFIED',
        'provider_billed_cost_usd':'.04','provider_billed_cost_verified':True,
        'provider_billed_currency':'USD','provider_billed_cost_source':'fixture:official-billing-reconciliation'}
    assert summarize_llm_costs([row])['llm_recorded_verified_usd']==Decimal('.04')
    assert summarize_llm_costs([row,{'id':'call-2','cost_usd':'.01'}])['llm_recorded_verified_usd'] is None
