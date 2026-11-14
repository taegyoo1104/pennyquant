"""LLM 잔액·연결 장애와 충전 메모가 기술적 매매·청산을 막지 않는지 검증한다."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.dashboard.summary_panels import cost_summary
from sessionquant.llm_health import response_health
from sessionquant.macro.analysis import BudgetedAnalyzer
from sessionquant.macro.models import MacroArticle
from sessionquant.macro.service import NewsPipeline

NOW = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)


def pipeline(repo):
    news = object.__new__(NewsPipeline)
    news.repo = repo
    news._analyses, news._economic = {}, {}
    return news


@pytest.mark.parametrize('code', ['insufficient_quota', 'credit_balance_exhausted'])
async def test_exhausted_credit_warns_and_cools_down_across_restart(tmp_path, code):
    repo = Repository(tmp_path/'news.db', backend='sqlite')
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, json={'error': {'code': code}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        analyzer = BudgetedAnalyzer(repo, api_key='mock-key', client=client, clock=lambda: NOW)
        article = MacroArticle('https://example.com/news', 'Market update', 'Test article body', 'fixture', NOW, NOW)
        result = await analyzer.analyze_macro(article)
        assert result['status'] == 'UNKNOWN' and result['force_liquidation'] is False
        assert repo.get_control('llm_provider_health')['code'] == 'NO_CREDIT'
        assert any('잔액' in row['message'] for row in repo.all('alerts'))
        assert len(repo.all('llm_usage')) == 1
        assert Decimal(repo.all('llm_usage')[0]['cost_usd']) == 0
        restarted = BudgetedAnalyzer(repo, api_key='mock-key', client=client, clock=lambda: NOW+timedelta(seconds=1))
        again = await restarted.analyze_macro(article)
        assert again['error'] == 'llm_budget_limit'
        assert len(calls) == 1 and len(repo.all('llm_usage')) == 1
        assert not pipeline(repo).risk_context(NOW, 'AAPL')['block_entries']
        # 충전 후에는 제한 시간이 지나면 같은 키로 분석을 재시도할 수 있다.
        restarted._clock = lambda: NOW+timedelta(minutes=11)
        await restarted.analyze_macro(article)
        assert len(calls) == 2
    repo.close()


def test_rate_limit_and_auth_are_not_reported_as_zero_credit(tmp_path):
    repo = Repository(tmp_path/'news.db', backend='sqlite')
    try:
        response_health(repo, httpx.Response(429, json={'error': {'code': 'rate_limit_exceeded'}}), key='mock', now=NOW)
        assert repo.get_control('llm_provider_health')['code'] == 'RATE_LIMIT'
        response_health(repo, httpx.Response(401), key='mock', now=NOW)
        assert repo.get_control('llm_provider_health')['code'] == 'AUTH_ERROR'
        response_health(repo, httpx.Response(200), key='mock', now=NOW)
        assert repo.get_control('llm_provider_health')['code'] == 'AVAILABLE'
    finally:
        repo.close()


def test_costs_are_decimal_and_missing_funding_is_not_zero_balance(tmp_path):
    repo = Repository(tmp_path/'costs.db', backend='sqlite')
    try:
        initial = cost_summary(repo, NOW)
        assert initial['spent'] is None and initial['deposited'] is None
        repo.set_control('session_llm_budget_v1', {'2026-10': {'spent': '0.1'}, '2026-11': {'spent': '0.2'}})
        repo.set_control('openai_funding_note', {'total_usd': '0'})
        summary = cost_summary(repo, NOW)
        assert summary['spent'] == Decimal('0.3') and summary['month'] == Decimal('0.2')
        assert summary['deposited'] == 0 and summary['health'] == {}
    finally:
        repo.close()


@pytest.mark.parametrize('credit_state', ['NO_KEY', 'NO_CREDIT'])
async def test_chart_entry_and_same_day_exit_with_no_llm_credit(tmp_path, credit_state):
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_demo import synthetic_tape
    from sessionquant.session_engine import SessionEngine, event_time
    from sessionquant.llm_health import set_health
    rows, opened = synthetic_tape()
    repo = Repository(tmp_path/'paper.db', backend='sqlite')
    universe = UniverseStore(tmp_path/'universe.json')
    universe.refresh(['AAPL'], source='SYNTHETIC_FIXTURE', observed_at=opened-timedelta(days=1))
    try:
        if credit_state == 'NO_KEY':
            analyzer = BudgetedAnalyzer(repo, api_key='', clock=lambda: NOW)
            await analyzer.aclose()
        else:
            set_health(repo, credit_state, now=NOW, key='mock', retry_seconds=600)
        repo.set_control('openai_funding_note', {'total_usd': '0'})
        engine = SessionEngine(repo, universe, synthetic=True, risk_provider=pipeline(repo))
        index = 0
        while index < len(rows):
            now = event_time(rows[index])
            end = index + 1
            while end < len(rows) and event_time(rows[end]) == now:
                end += 1
            engine.apply_batch(rows[index:end], now, cursor=end)
            index = end
        assert [fill.side for fill in engine.broker.fills] == ['buy', 'sell']
        assert not engine.broker.positions
        assert any(order.intent.reason == 'session_exit' for order in engine.broker.orders.values())
        assert repo.all('llm_usage') == []
    finally:
        repo.close()


def test_previously_confirmed_bad_news_remains_blocking_after_quota_failure(tmp_path):
    from sessionquant.llm_health import set_health
    repo = Repository(tmp_path/'risk.db', backend='sqlite')
    try:
        p = pipeline(repo)
        p._analyses['bad'] = {'category': 'company', 'ticker': 'AAPL', 'published_at': NOW.isoformat(),
            'analyzed_at': NOW.isoformat(), 'primary_verified': True, 'requires_verification': False,
            'confidence': .95, 'missing_information': [], 'error': '',
            'events': [{'status': 'CONFIRMED', 'importance': 'HIGH', 'shareholder_impact': 'NEGATIVE'}]}
        set_health(repo, 'NO_CREDIT', now=NOW, key='mock', retry_seconds=600)
        context = p.risk_context(NOW, 'AAPL')
        assert context['block_entries'] and context['major_adverse']
    finally:
        repo.close()


async def test_recorded_zero_funding_does_not_prevent_company_api_call(tmp_path):
    from sessionquant.models import NewsEvent
    repo = Repository(tmp_path/'company.db', backend='sqlite')
    repo.set_control('openai_funding_note', {'total_usd': '0'})
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, json={'error': {'code': 'insufficient_quota'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        analyzer = BudgetedAnalyzer(repo, api_key='mock-key', client=client, clock=lambda: NOW)
        event = NewsEvent('rss:fixture', ('AAPL',), 'Company publishes quarterly results',
            NOW-timedelta(minutes=1), NOW-timedelta(seconds=1), 'Company IR',
            'https://issuer.test/ir', 'The company reported its quarterly revenue and earnings.', True, False)
        result = await analyzer.analyze(event, 'AAPL', source_kind='rss', primary_verified=True)
        assert len(calls) == 1 and result.error == 'llm_http_429'
        assert '차트' in result.reason and not result.entry_eligible
        again = await analyzer.analyze(event, 'AAPL', source_kind='rss', primary_verified=True)
        assert again.error == 'llm_budget_limit' and len(calls) == 1
    repo.close()
