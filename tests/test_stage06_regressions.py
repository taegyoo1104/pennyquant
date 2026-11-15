"""06단계의 분석 경계·동시성·수정 이력·시점별 표시를 검증한다."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from sessionquant.database.repository import Repository
from sessionquant.macro.context import build_context
from sessionquant.macro.models import EconomicEvent, MacroArticle, stable_hash
from sessionquant.macro.service import NewsPipeline
from sessionquant.models import NewsEvent
from sessionquant.news.schedule import NewsSchedule
from sessionquant.news.service import NewsService
from sessionquant.session_config import load_session_config

NOW = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)


@pytest.fixture
def repo(tmp_path):
    value = Repository(tmp_path/'stage06.db', backend='sqlite')
    yield value
    value.close()


@pytest.mark.parametrize('changes,expected', [
    ({'body': ''}, 'article_body_missing'),
    ({'verified': False}, 'unverified_article_identity'),
    ({'symbols': ('MSFT',)}, 'ticker_not_in_article_symbols'),
    ({'published_at': NOW-timedelta(hours=2)}, 'stale_article'),
])
async def test_service_itself_blocks_before_permissive_or_paid_analyzer(repo, changes, expected):
    analyzer = SimpleNamespace(max_age_hours=1, analyze=AsyncMock(), aclose=AsyncMock())
    service = NewsService({}, analyzer=analyzer, repository=repo, include_alpaca=False, clock=lambda: NOW)
    event = NewsEvent('alpaca:1', ('AAPL',), 'Results', NOW-timedelta(minutes=1), NOW,
                      'Benzinga', 'https://www.benzinga.com/fixture', 'Full verified text.', True, False)
    try:
        result = await service._analyze(replace(event, **changes), 'AAPL', 'alpaca', False, NOW)
        assert result.sentiment == 'UNKNOWN' and result.requires_verification and not result.entry_eligible
        assert result.error == expected
        analyzer.analyze.assert_not_called()
        ingestion = repo.all('news_ingestions')[0]
        assert ingestion['skip_reason'] == expected and not ingestion['eligible_for_llm']
    finally:
        await service.aclose()


def test_sqlite_schedule_atomic_claim_and_counters_across_connections(tmp_path):
    path = tmp_path/'parallel.db'
    Repository(path, backend='sqlite').close()
    barrier = Barrier(2)
    def worker(_):
        connection = Repository(path, backend='sqlite')
        schedule = NewsSchedule(connection, clock=lambda: NOW)
        barrier.wait()
        claimed = schedule.claim('gdelt', 900, NOW)
        for _ in range(25):
            schedule.begin('rss', NOW)
        connection.close()
        return claimed
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, range(2))) == [False, True]
    connection = Repository(path, backend='sqlite')
    try:
        restored = NewsSchedule(connection, clock=lambda: NOW)
        assert restored.state['gdelt']['calls'] == 1
        assert restored.state['rss']['calls'] == 50
        assert not restored.claim('gdelt', 0, NOW+timedelta(seconds=299))
        assert restored.claim('gdelt', 0, NOW+timedelta(seconds=300))
    finally:
        connection.close()


@pytest.mark.parametrize('retry', ['Fri, 13 Nov 2026 18:00:00 GMT', '7200', '1e999', 'invalid'])
def test_retry_after_date_and_invalid_values_are_bounded(repo, retry):
    request = httpx.Request('GET', 'https://api.gdeltproject.org/')
    error = httpx.HTTPStatusError('fixture', request=request,
        response=httpx.Response(429, request=request, headers={'Retry-After': retry}))
    schedule = NewsSchedule(repo, clock=lambda: NOW, jitter=lambda: 0)
    schedule.failure('gdelt', error)
    restored = NewsSchedule(repo, clock=lambda: NOW)
    delay = 7200 if retry in {'7200', 'Fri, 13 Nov 2026 18:00:00 GMT'} else 600
    assert not restored.due('gdelt', 0, NOW+timedelta(seconds=delay-1))
    assert restored.due('gdelt', 0, NOW+timedelta(seconds=delay))


def minimal_pipeline(repo):
    pipeline = object.__new__(NewsPipeline)
    pipeline.repo, pipeline._analyses, pipeline._economic = repo, {}, {}
    pipeline._restore()
    return pipeline


def test_calendar_revisions_cancel_restart_and_first_knowledge(repo):
    pipeline = minimal_pipeline(repo)
    original = EconomicEvent('original', 'Employment Situation', NOW, NOW-timedelta(hours=2),
        'https://www.bls.gov/schedule/news_release/bls.ics', verified=True, logical_id='jobs')
    revised = replace(original, id='revision', revision=1, scheduled_at=NOW+timedelta(hours=1),
                      known_at=NOW+timedelta(minutes=2))
    cancelled = replace(revised, id='cancelled', revision=2, status='CANCELLED', known_at=NOW+timedelta(minutes=30))
    for value in (original, revised, cancelled, replace(original, known_at=NOW+timedelta(days=1))):
        pipeline.add_economic_event(value)
    restored = minimal_pipeline(repo)
    assert restored.risk_context(NOW)['blackout']
    assert not restored.risk_context(NOW+timedelta(minutes=2))['blackout']
    assert not restored.risk_context(NOW+timedelta(hours=1))['blackout']
    assert repo.get_record('economic_events', 'original')['known_at'] == original.known_at.isoformat()
    assert len(repo.all('economic_events')) == 3
    assert restored.recorded_evidence(NOW, 'AAPL')['economic_events'][0]['id'] == 'original'
    assert restored.economic_events_at(NOW+timedelta(hours=1))[0].status == 'CANCELLED'


@pytest.mark.parametrize('changes', [
    {'status': 'UNKNOWN'}, {'revision': -1}, {'revision': True}, {'importance': 'EXTREME'},
    {'verified': 'yes'}, {'known_at': datetime(2026, 11, 13)},
    {'scheduled_local': '2026-11-13T12:00:00-05:00'},
])
def test_economic_event_rejects_invalid_state_or_time(changes):
    args = dict(id='event', name='CPI', scheduled_at=NOW, known_at=NOW,
                source_url='https://www.bls.gov/news.release/cpi.htm', verified=True)
    with pytest.raises((ValueError, TypeError)):
        EconomicEvent(**(args | changes))


def test_context_uses_real_etf_dict_and_preserves_only_verified_windows(repo):
    from sessionquant.market_reference.store import ReferenceStore
    from test_market_reference import series, NOW as reference_now
    series(ReferenceStore(repo))
    value = build_context(repo, reference_now)
    assert len(value['intraday_market_proxies']) == 1
    proxy = value['intraday_market_proxies'][0]
    assert proxy['symbol'] == 'IEF' and proxy['role'] == 'ETF_PROXY_ONLY'
    assert proxy['windows']['30']['sample_count'] == 31
    assert not value['causal_inference_verified']
    assert not build_context(repo, reference_now-timedelta(seconds=1))['intraday_market_proxies']


@pytest.mark.parametrize('case', ['missing_start', 'future_available', 'wrong_window', 'samples', 'expired', 'empty'])
def test_context_rejects_inconsistent_etf_windows(repo, case):
    from sessionquant.market_reference.store import ReferenceStore, VIEW
    from test_market_reference import series, NOW as reference_now
    series(ReferenceStore(repo))
    view = deepcopy(repo.get_control(VIEW))
    row = view['items']['IEF']
    if case == 'missing_start': row['windows']['30'].pop('start_at')
    if case == 'future_available': row['windows']['30']['available_at'] = (reference_now+timedelta(seconds=1)).isoformat()
    if case == 'wrong_window': row['windows']['30']['start_at'] = reference_now.isoformat()
    if case == 'samples': row['windows']['30']['sample_count'] = 2
    if case == 'expired': row['valid_until'] = (reference_now-timedelta(seconds=1)).isoformat()
    if case == 'empty': row['windows'] = {}
    repo.set_control(VIEW, view)
    snapshot = build_context(repo, reference_now)
    assert snapshot['intraday_market_proxies'] == []
    assert 'proxy_time_mismatch_rejected' in snapshot['reasons']


def test_macro_default_fields_do_not_change_stage05_content_hash():
    article = MacroArticle('https://www.bls.gov/release', 'Title', 'Body', 'BLS', NOW, NOW, official=True)
    legacy = stable_hash({'title': 'Title', 'body': 'Body', 'topic': 'unknown',
        'published_at': NOW.isoformat(), 'official': True, 'source_family': ''})
    assert article.content_hash == legacy
    assert replace(article, provider_seen_at='20261113160000').content_hash == legacy
    assert replace(article, document_version='revised').content_hash != legacy


@pytest.mark.parametrize('value', [300.0, 600.5])
async def test_legacy_float_interval_constructs_pipeline_and_preserves_new_key(tmp_path, repo, value):
    path = tmp_path/'config.yaml'
    path.write_text(f'company_rss_catalog: false\nnews_refresh_seconds: {value}\ncompany_news_refresh_seconds: 180\n')
    config = load_session_config(path)
    assert config.company_news_refresh_seconds == 180 and config.macro_news_refresh_seconds == value
    pipeline = NewsPipeline(repo, macro_refresh_seconds=config.macro_news_refresh_seconds)
    await pipeline.aclose()


@pytest.mark.parametrize('value', ['true', '.nan', '.inf', '179', '86401'])
def test_new_interval_invalid_values_are_rejected(tmp_path, value):
    path = tmp_path/'config.yaml'
    path.write_text(f'company_news_refresh_seconds: {value}\n')
    with pytest.raises(ValueError): load_session_config(path)


def test_late_provider_result_cannot_clear_newer_attempt_cooldown(repo):
    first = NewsSchedule(repo, clock=lambda: NOW)
    second = NewsSchedule(repo, clock=lambda: NOW+timedelta(minutes=6), jitter=lambda: 0)
    assert first.claim('gdelt', 0)
    assert second.claim('gdelt', 0)
    request = httpx.Request('GET', 'https://api.gdeltproject.org/')
    error = httpx.HTTPStatusError('fixture', request=request, response=httpx.Response(429, request=request))
    second.failure('gdelt', error)
    first.success('gdelt', 1, NOW+timedelta(minutes=7))
    restored = NewsSchedule(repo)
    assert restored.state['gdelt']['status'] == 'PROVIDER_RATE_LIMITED'
    assert not restored.due('gdelt', 0, NOW+timedelta(minutes=15))


@pytest.mark.parametrize('changes', [
    {'received_at': (NOW+timedelta(seconds=1)).isoformat()},
    {'ingested_at': (NOW-timedelta(seconds=1)).isoformat()},
    {'observation_date': '2026-11-14'},
    {'published_at': (NOW+timedelta(seconds=1)).isoformat(), 'point_in_time_verified': True},
])
def test_context_rejects_future_or_inverted_daily_reference(repo, changes):
    repo.record('fred_reference_observations', {'id': 'daily', 'series': 'DGS10', 'value': '4.3',
        'observation_date': '2026-11-12', 'received_at': NOW.isoformat(), 'ingested_at': NOW.isoformat(),
        'published_at': None, 'point_in_time_verified': False} | changes)
    snapshot = build_context(repo, NOW)
    assert snapshot['official_daily_indicators'] == []
    assert 'fred_time_mismatch_rejected' in snapshot['reasons']


async def test_gdelt_two_topic_budget_rotation_and_minimum_interval(repo):
    from sessionquant.macro.sources import GDELTClient
    calls = []
    def handle(request):
        calls.append(request.url.params['query'])
        return httpx.Response(200, json={'articles': []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        first = GDELTClient(client=http, repository=repo)
        first.limiter.interval = 0
        await first.fetch(now=NOW)
        assert len(calls) == 2
        await first.fetch(now=NOW+timedelta(minutes=14))
        assert len(calls) == 2
        restored = GDELTClient(client=http, repository=repo)
        restored.limiter.interval = 0
        await restored.fetch(now=NOW+timedelta(minutes=15))
        assert len(calls) == 4 and len(set(calls)) == 4


async def test_pipeline_retries_failed_analysis_then_deduplicates_completed_version(repo):
    clock = [NOW]
    article = MacroArticle('https://www.bls.gov/release', 'Official employment release', 'Full official release.',
                           'BLS', NOW-timedelta(hours=1), NOW, official=True)
    class Empty:
        status = []
        async def fetch(self, **_): return []
        async def aclose(self): pass
    class Official(Empty):
        async def fetch(self): return [replace(article, received_at=clock[0])]
    class Company(Empty):
        events = []
        async def refresh(self, symbols, now): return []
    calls = []
    async def analyze(value, now):
        calls.append(value)
        return {'id': 'analysis:'+str(len(calls)), 'article_id': value.id,
            'published_at': value.published_at.isoformat(), 'received_at': value.received_at.isoformat(),
            'analyzed_at': now.isoformat(), 'status': 'UNKNOWN', 'force_liquidation': False,
            'error': 'llm_budget_limit' if len(calls) == 1 else ''}
    analyzer = SimpleNamespace(analyze_macro=analyze, ledger=SimpleNamespace(snapshot=lambda now: {}))
    pipeline = NewsPipeline(repo, analyzer=analyzer, company_service=Company(), official_client=Official(),
        gdelt_client=Empty(), treasury_client=Empty(), fred_client=Empty(), clock=lambda: clock[0])
    try:
        await pipeline.refresh([], now=clock[0])
        clock[0] += timedelta(minutes=15)
        await pipeline.refresh([], now=clock[0])
        assert len(calls) == 2
        clock[0] += timedelta(minutes=15)
        await pipeline.refresh([], now=clock[0])
        assert len(calls) == 2
        assert any(row.get('error') == 'duplicate_article' for row in repo.all('macro_analysis'))
    finally:
        await pipeline.aclose()
