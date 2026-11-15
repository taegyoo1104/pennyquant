"""키 추가 저장과 주문·LLM 생성 없는 연결 점검을 검증한다."""
import json
from pathlib import Path
import httpx
import pytest

from sessionquant.local_env import setup_local_env,load_local_env
from sessionquant.news_setup import check_news


def config_file(tmp_path):
    path=tmp_path/'session.yaml'; path.write_text('company_rss_catalog: false\ncompanies: {}\nllm: {}\n',encoding='utf-8')
    return path


def test_news_wizard_preserves_existing_connections_and_hides_keys(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('SQ_MYSQL_PASSWORD','db-private')
    monkeypatch.setenv('TOSS_CLIENT_SECRET','toss-private')
    secrets=iter(['openai-private','fred-private','alpaca-key-private','alpaca-secret-private'])
    path=setup_local_env(news_only=True,path=tmp_path/'.env',secret_fn=lambda prompt:next(secrets),
        input_fn=lambda prompt:'SessionQuant Owner owner@example.com')
    text=path.read_text(); output=capsys.readouterr().out
    for value in ['db-private','toss-private','openai-private','fred-private','alpaca-key-private','alpaca-secret-private']:
        assert value in text and value not in output


async def test_offline_check_does_not_call_network_or_use_legacy_model(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENAI_MODEL','legacy-different')
    monkeypatch.setenv('OPENAI_API_KEY','hidden-value')
    monkeypatch.setattr(httpx.AsyncClient,'request',lambda *a,**k:pytest.fail('오프라인 점검에서 네트워크 호출'))
    report=await check_news(config_file(tmp_path))
    assert report['models']['primary']=='gpt-4.1-mini'
    assert report['llm_generation_calls']==0 and report['live_orders_enabled'] is False
    assert report['configured']['OPENAI_API_KEY'] is True
    assert 'hidden-value' not in json.dumps(report)


async def test_model_access_probe_only_gets_model_metadata(tmp_path,monkeypatch):
    import sessionquant.news_setup as module
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.method=='GET' and request.url.path.startswith('/v1/models/')
        return httpx.Response(200 if request.url.path.endswith('gpt-4.1-mini') else 404,json={'id':'mock'})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setenv('OPENAI_API_KEY','mock-secret')
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:client)
    report=await check_news(config_file(tmp_path),connect=True,provider='openai')
    values=report['connections']['openai']
    assert len(requests)==2 and values['models'][0]['accessible'] is True
    assert values['models'][1]['accessible'] is False
    assert values['structured_generation_verified'] is False
    assert 'mock-secret' not in json.dumps(report)


async def test_fred_error_never_prints_api_key_in_request_url(tmp_path,monkeypatch):
    import sessionquant.news_setup as module
    class FakeFred:
        async def fetch(self):
            request=httpx.Request('GET','https://api.stlouisfed.org/fred/series/observations?api_key=mock-private')
            response=httpx.Response(401,request=request)
            response.raise_for_status()
        async def aclose(self): pass
    monkeypatch.setenv('FRED_API_KEY','mock-private')
    monkeypatch.setattr(module,'FREDClient',FakeFred)
    report=await check_news(config_file(tmp_path),connect=True,provider='fred')
    assert report['connections']['fred']=={'status':'error','http_status':401}
    assert 'mock-private' not in json.dumps(report)


async def test_empty_rss_mapping_is_not_reported_as_configured(tmp_path):
    report=await check_news(config_file(tmp_path),connect=True,provider='rss')
    assert report['connections']['rss']['status']=='not_configured'


async def test_offline_reports_independent_cadence_calendar_and_default_rights(tmp_path):
    report = await check_news(config_file(tmp_path))
    assert report['intervals_seconds'] == {'company': 180, 'macro': 900, 'calendar': 3600, 'fred': 86400}
    assert report['new_analysis_max_age_hours'] == {'company': 24, 'macro': 72}
    assert set(report['calendar']['automatic_feeds']) == {'bls', 'bea'}
    assert report['enrichment']['configured_publishers'] == 0
    assert not report['enrichment']['probe_fetches_articles']


async def test_calendar_probe_uses_only_get_and_reports_403_without_llm(tmp_path, monkeypatch):
    from sessionquant.macro.calendar import EconomicCalendarClient
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(403)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = EconomicCalendarClient(client=http)
    client.limiter.interval = 0
    monkeypatch.setattr('sessionquant.macro.calendar.EconomicCalendarClient', lambda: client)
    try:
        report = await check_news(config_file(tmp_path), connect=True, provider='calendar')
        assert len(calls) == 2 and all(r.method == 'GET' for r in calls)
        assert report['connections']['calendar']['status'] == 'unavailable'
        assert report['llm_generation_calls'] == 0 and not report['live_orders_enabled']
        assert all(row['http_status'] == 403 for row in report['connections']['calendar']['feeds'] if row['provider'] in {'bls', 'bea'})
    finally:
        await http.aclose()
