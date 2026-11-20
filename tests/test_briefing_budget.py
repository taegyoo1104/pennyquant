"""News cannot consume the briefing slice or bypass total/monthly limits."""
from datetime import datetime, timedelta

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.macro.models import LLMSettings
from sessionquant.macro.budget import BudgetLedger, BudgetBlocked, CONTROL
from sessionquant.macro.analysis import BudgetedAnalyzer

NOW = datetime.fromisoformat('2026-11-20T14:40:00+00:00')
BRIEFING = {'source_kind':'strategy_snapshot'}


@pytest.fixture
def repo(tmp_path):
    repo = Repository(tmp_path / 'budget.db', backend='sqlite')
    yield repo
    repo.close()


def settings(**changes):
    return LLMSettings(**{'daily_primary_call_cap':6, 'hourly_primary_call_cap':4,
        'daily_briefing_reserved_calls':2, 'hourly_briefing_reserved_calls':2, **changes})


async def test_news_leaves_calls_for_briefing_across_restart(repo):
    ledger = BudgetLedger(repo, settings())
    for _ in range(2):
        await ledger.reserve('primary', {}, NOW)
    with pytest.raises(BudgetBlocked, match='브리핑'):
        await ledger.reserve('primary', {}, NOW)
    restored = BudgetLedger(repo, settings())
    for _ in range(2):
        await restored.reserve('primary', {}, NOW, metadata=BRIEFING)
    with pytest.raises(BudgetBlocked) as blocked:
        await restored.reserve('primary', {}, NOW, metadata=BRIEFING)
    assert blocked.value.code == 'HOURLY_CALL_CAP'
    later = NOW + timedelta(hours=1)
    for _ in range(2):
        await restored.reserve('primary', {}, later)
    with pytest.raises(BudgetBlocked) as blocked:
        await restored.reserve('primary', {}, later, metadata=BRIEFING)
    assert blocked.value.code == 'DAILY_CALL_CAP'
    assert restored.snapshot(later)['daily_calls']['primary'] == 6


async def test_daily_reserve_and_monthly_cost_limit_are_independent(repo):
    ledger = BudgetLedger(repo, settings(hourly_primary_call_cap=10))
    for _ in range(4):
        await ledger.reserve('primary', {}, NOW)
    with pytest.raises(BudgetBlocked) as blocked:
        await ledger.reserve('primary', {}, NOW)
    assert blocked.value.code == 'BRIEFING_DAILY_RESERVE'
    no_money = BudgetLedger(repo, settings(hourly_primary_call_cap=10,
        monthly_soft_budget_usd='0', monthly_hard_budget_usd='0'))
    with pytest.raises(BudgetBlocked) as blocked:
        await no_money.reserve('primary', {}, NOW, metadata=BRIEFING)
    assert blocked.value.code == 'MONTHLY_COST_CAP'


async def test_existing_600_calls_are_not_reset_or_bypassed(repo):
    repo.set_control(CONTROL, {'2026-11':{'spent':'0.5770024', 'reservations':{},
        'days':{'2026-11-20':{'primary':600, 'escalation':0}}}})
    ledger = BudgetLedger(repo, settings(daily_primary_call_cap=600, hourly_primary_call_cap=120,
                                         daily_briefing_reserved_calls=80, hourly_briefing_reserved_calls=12))
    with pytest.raises(BudgetBlocked) as blocked:
        await ledger.reserve('primary', {}, NOW, metadata=BRIEFING)
    assert blocked.value.code == 'DAILY_CALL_CAP'
    assert ledger.snapshot(NOW)['daily_calls']['primary'] == 600
    await ledger.reserve('primary', {}, NOW+timedelta(days=1), metadata=BRIEFING)
    assert ledger.snapshot(NOW+timedelta(days=1))['daily_calls']['primary'] == 1
    assert ledger.snapshot(NOW)['daily_calls']['primary'] == 600


async def test_real_adapter_passes_briefing_purpose_to_shared_budget(repo):
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'usage':{'prompt_tokens':1,'completion_tokens':1}})
    config = settings(daily_primary_call_cap=2, daily_briefing_reserved_calls=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer = BudgetedAnalyzer(repo, config, api_key='fixture', client=client, clock=lambda:NOW)
        with analyzer.audit.scope({'source_kind':'company_news'}):
            with pytest.raises(BudgetBlocked):
                await analyzer._adapters['primary'].post('https://fixture.invalid/', json={'model':config.primary_model})
        assert calls == []
        with analyzer.audit.scope(BRIEFING):
            await analyzer._adapters['primary'].post('https://fixture.invalid/', json={'model':config.primary_model})
    assert len(calls) == 1
    assert repo.get_control(CONTROL)['2026-11']['briefing_days']['2026-11-20'] == 1
    assert not repo.all('orders')


def hourly_settings(**changes):
    return LLMSettings(**{'daily_primary_call_cap':600,'hourly_primary_call_cap':120,
        'monthly_soft_budget_usd':'3','monthly_hard_budget_usd':'3',
        'briefing_separate_schedule':True,**changes})


async def test_hourly_briefing_works_after_600_news_calls_without_resetting_usage(repo):
    repo.set_control(CONTROL, {'2026-11':{'spent':'0.5770024','reservations':{},
        'days':{'2026-11-20':{'primary':600,'escalation':0}},
        'hours':{'2026-11-20T14':{'primary':120,'escalation':0}}}})
    ledger = BudgetLedger(repo,hourly_settings())
    await ledger.reserve('primary',{},NOW,metadata=BRIEFING)
    result = ledger.snapshot(NOW)
    assert result['daily_calls']['primary'] == 601 and result['daily_news_calls'] == 600
    assert result['daily_briefing_calls'] == 1 and result['spent_usd'] == '0.5770024'
    with pytest.raises(BudgetBlocked) as blocked:
        await ledger.reserve('primary',{},NOW+timedelta(minutes=1),metadata={'source_kind':'company_news'})
    assert blocked.value.code == 'DAILY_CALL_CAP'
    restored = BudgetLedger(repo,hourly_settings())
    with pytest.raises(BudgetBlocked) as blocked:
        await restored.reserve('primary',{},NOW+timedelta(minutes=59),metadata=BRIEFING)
    assert blocked.value.code == 'BRIEFING_INTERVAL'
    await restored.reserve('primary',{},NOW+timedelta(hours=1),metadata=BRIEFING)
    assert restored.snapshot(NOW)['daily_calls']['primary'] == 602


@pytest.mark.parametrize('first', ['2026-11-20T23:59:00+00:00','2026-11-30T23:59:00+00:00'])
async def test_hourly_interval_survives_day_and_month_boundaries(repo, first):
    now = datetime.fromisoformat(first)
    await BudgetLedger(repo,hourly_settings()).reserve('primary',{},now,metadata=BRIEFING)
    other = BudgetLedger(repo,hourly_settings())
    with pytest.raises(BudgetBlocked) as blocked:
        await other.reserve('primary',{},now+timedelta(minutes=2),metadata=BRIEFING)
    assert blocked.value.code == 'BRIEFING_INTERVAL'
    await other.reserve('primary',{},now+timedelta(hours=1),metadata=BRIEFING)


async def test_separate_briefing_still_obeys_monthly_cost_and_pending_reservations(repo):
    ledger = BudgetLedger(repo,hourly_settings(monthly_soft_budget_usd='0.004',monthly_hard_budget_usd='0.004'))
    await ledger.reserve('primary',{},NOW,metadata=BRIEFING)
    with pytest.raises(BudgetBlocked) as blocked:
        await ledger.reserve('primary',{},NOW+timedelta(hours=1),metadata=BRIEFING)
    assert blocked.value.code == 'MONTHLY_COST_CAP'
    assert ledger.snapshot(NOW)['daily_calls']['primary'] == 1


async def test_briefing_does_not_consume_news_quota_or_allow_escalation_bypass(repo):
    ledger = BudgetLedger(repo,hourly_settings(daily_primary_call_cap=1,hourly_primary_call_cap=1,
                                              daily_escalation_call_cap=0))
    await ledger.reserve('primary',{},NOW,metadata=BRIEFING)
    await ledger.reserve('primary',{},NOW,metadata={'source_kind':'company_news'})
    with pytest.raises(BudgetBlocked):
        await ledger.reserve('primary',{},NOW)
    with pytest.raises(BudgetBlocked):
        await ledger.reserve('escalation',{},NOW,metadata=BRIEFING)


@pytest.mark.parametrize('value', [-1, 7, True, '2'])
def test_invalid_reserved_calls_rejected(value):
    with pytest.raises(ValueError):
        settings(daily_briefing_reserved_calls=value)
