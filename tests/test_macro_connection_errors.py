"""뉴스 소스 오류를 성공과 구분하고 인증정보 없는 진단만 출력한다."""
from datetime import datetime, timezone
import json

import httpx
import pytest

from sessionquant.collectors.rss_collector import RSSCollector, parse_feed
from sessionquant.collectors.common import DataQualityError
from sessionquant.macro.sources import FREDClient, GDELTClient, OfficialNewsClient
from sessionquant.news_setup import check_news

NOW = datetime(2026, 11, 14, 14, tzinfo=timezone.utc)
FEED = b'''<?xml version="1.0"?><rss version="2.0"><channel><item>
<title>Official update</title><link>https://www.eia.gov/pressroom/release.php?id=1</link>
<pubDate>Fri, 13 Nov 2026 15:00:00 GMT</pubDate><description>Official release</description>
</item></channel></rss>'''


async def test_rss_requests_text_xml_used_by_official_feeds():
    def handle(request):
        # BEA/EIA처럼 text/xml을 허용하지 않은 요청에는 406을 반환한다.
        if 'text/xml' not in request.headers['Accept']:
            return httpx.Response(406)
        return httpx.Response(200, content=FEED, headers={'Content-Type': 'text/xml'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        rss = RSSCollector(client=client)
        rows = await rss.fetch('https://www.eia.gov/rss/press_rss.xml', source='EIA', verified=True)
        assert len(rows) == 1 and rows[0].title == 'Official update'


def test_xml_shaped_html_error_page_is_not_empty_successful_feed():
    with pytest.raises(DataQualityError):
        parse_feed(b'<html><body>Access denied</body></html>', source='test', received_at=NOW)


@pytest.mark.parametrize(('server_message', 'code'), [
    ('Bad Request. The value for variable api_key is not registered.', 'api_key_rejected'),
    ('The value for variable api_key is not a 32 character alpha-numeric lower-case string.', 'invalid_api_key_format'),
    ('The value for variable realtime_start is invalid.', 'invalid_realtime_window'),
    ('The value for variable series_id is not found.', 'invalid_series_id'),
    ('An unknown error occurred.', 'request_rejected'),
])
async def test_fred_report_classifies_rejection_without_leaking_body_or_url(tmp_path, monkeypatch, server_message, code):
    import sessionquant.news_setup as module
    secret = 'private-fred-key'
    def handle(request):
        assert request.url.params['api_key'] == secret
        return httpx.Response(400, json={'error_code': 400, 'error_message': server_message+' '+secret})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        fred = FREDClient(api_key=secret, client=client)
        monkeypatch.setenv('FRED_API_KEY', secret)
        monkeypatch.setattr(module, 'FREDClient', lambda: fred)
        settings = tmp_path/'config.yaml'
        settings.write_text('companies: {}\n')
        result = (await check_news(settings, connect=True, provider='fred'))['connections']['fred']
        assert result['status'] == 'error' and result['http_status'] == 400
        assert result['error_code'] == code
        assert secret not in json.dumps(result) and server_message not in json.dumps(result)


async def test_fred_success_keeps_asof_date_and_daily_auxiliary_semantics():
    def handle(request):
        assert request.url.params['realtime_start'] == '2026-11-14'
        assert request.url.params['realtime_end'] == '2026-11-14'
        return httpx.Response(200, json={'observations': [{'date': '2026-11-13', 'value': '4.10'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        fred = FREDClient(api_key='mock-fred-key', client=client)
        rows = await fred.fetch(now=NOW)
        assert {r['series'] for r in rows} == set(FREDClient.SERIES)
        assert all(r['auxiliary_only'] and not r['real_time'] and r['value'] == '4.10' for r in rows)


async def test_official_partial_failure_reports_status_without_request_details(tmp_path, monkeypatch):
    import sessionquant.news_setup as module
    feeds = {'eia': ('EIA', 'https://www.eia.gov/rss/press_rss.xml', 'oil_supply_disruption'),
             'bls': ('BLS', 'https://www.bls.gov/feed/bls_latest.rss?key=private', 'inflation_and_employment')}
    def handle(request):
        return httpx.Response(403) if request.url.host == 'www.bls.gov' else httpx.Response(200, content=FEED)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        official = OfficialNewsClient(rss=RSSCollector(client=client), feeds=feeds)
        monkeypatch.setattr(module, 'OfficialNewsClient', lambda: official)
        settings = tmp_path/'config.yaml'
        settings.write_text('companies: {}\n')
        result = (await check_news(settings, connect=True, provider='official'))['connections']['official']
        assert result['status'] == 'partial' and result['events'] == 1
        assert result['feeds'][1]['http_status'] == 403
        assert 'private' not in json.dumps(result)


async def test_gdelt_failed_search_differs_from_successful_empty_search(tmp_path, monkeypatch):
    import sessionquant.news_setup as module
    for broken in (True, False):
        def handle(request):
            # JSON이 아닌 오류 응답과 유효한 빈 검색 결과를 구분한다.
            return httpx.Response(200, text='upstream unavailable') if broken else httpx.Response(200, json={'articles': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = GDELTClient(client=client, topics=('us_monetary_policy',))
            monkeypatch.setattr(module, 'GDELTClient', lambda **kwargs: source)
            settings = tmp_path/'config.yaml'
            settings.write_text('companies: {}\n')
            result = (await check_news(settings, connect=True, provider='gdelt'))['connections']['gdelt']
            assert result['status'] == ('unavailable' if broken else 'ok')
            assert result['events'] == 0
            if broken: assert result['topics'][0]['error_type'] == 'JSONDecodeError'
