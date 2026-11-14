"""공식 RSS 기본 목록의 출처·사용자 설정 우선순위와 전체 접속 점검을 검증한다."""
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from sessionquant.news.catalog import load_company_catalog
from sessionquant.session_config import SessionConfig, load_session_config
from sessionquant.news_setup import check_news


def test_catalog_entries_have_membership_cik_and_verification_evidence():
    catalog = load_company_catalog()
    members = catalog['universe_members']
    assert 450 <= len(members) <= 550 and len(set(members)) == len(members)
    assert set(catalog['companies']) <= set(members)
    assert set(catalog['companies']) == set(catalog['verification'])
    assert catalog['companies']
    for symbol, source in catalog['companies'].items():
        evidence = catalog['verification'][symbol]
        assert evidence['feed_url'] in source['rss_urls']
        assert evidence['http_status'] == 200 and evidence['parser_verified'] is True
        assert evidence['events_at_verification'] > 0 and evidence['verified_at']
        assert evidence['latest_published_at']
        assert evidence['discovery_chain'][-1] == evidence['feed_url']
        assert source['rss_verified'] is True and source['cik'].isdigit()
        for url in source['rss_urls']:
            parsed = urlsplit(url)
            assert parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
            assert 'comment' not in url.lower() and 'secfiling' not in url.lower()


def test_builtin_catalog_is_loaded_without_editing_user_config(tmp_path):
    path = tmp_path/'config.yaml'
    path.write_text('companies: {}\n', encoding='utf-8')
    config = load_session_config(path)
    assert config.company_rss_catalog is True
    assert config.companies == load_company_catalog()['companies']
    assert path.read_text() == 'companies: {}\n'


def test_user_fields_override_defaults_and_can_disable_individual_feeds():
    config = SessionConfig(companies={
        'NVDA': {'rss_urls': ['https://nvidianews.nvidia.com/releases.xml'], 'rss_verified': False},
        'JNJ': {'rss_urls': []}})
    assert config.companies['NVDA']['rss_urls'] == ['https://nvidianews.nvidia.com/releases.xml']
    assert config.companies['NVDA']['rss_verified'] is False
    assert config.companies['NVDA']['cik'] == load_company_catalog()['companies']['NVDA']['cik']
    assert config.companies['JNJ']['rss_urls'] == []
    assert config.companies['JNJ']['company_name']


def test_catalog_can_be_disabled_without_removing_manual_company_settings():
    assert SessionConfig(company_rss_catalog=False).companies == {}
    manual = {'NVDA': {'rss_urls': ['https://nvidianews.nvidia.com/releases.xml'], 'rss_verified': True}}
    assert SessionConfig(company_rss_catalog=False, companies=manual).companies == manual
    with pytest.raises(ValueError):
        SessionConfig(company_rss_catalog='false')


async def test_rss_check_covers_more_than_ten_and_deduplicates_shared_feed(tmp_path, monkeypatch):
    import sessionquant.news_setup as module
    manual = {f'TEST{i}': {'rss_urls': [f'https://issuer.example/{max(0,i-1)}/feed']} for i in range(14)}
    path = tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump({'company_rss_catalog': False, 'companies': manual}))
    stats = {'active': 0, 'maximum': 0, 'closed': False, 'urls': []}
    class RSS:
        async def fetch(self, url, **kwargs):
            stats['urls'].append(url)
            stats['active'] += 1
            stats['maximum'] = max(stats['maximum'], stats['active'])
            try:
                await asyncio.sleep(.001)
                if '/12/' in url: raise ValueError('모의 잘못된 XML')
                return [object()]
            finally:
                stats['active'] -= 1
        async def aclose(self): stats['closed'] = True
    monkeypatch.setattr('sessionquant.collectors.rss_collector.RSSCollector', RSS)
    result = await module.check_news(path, connect=True, provider='rss')
    rss = result['connections']['rss']
    assert result['company_rss_count'] == 14
    assert rss['status'] == 'partial'
    assert rss['checked_feeds'] == rss['configured_feeds'] == len(rss['feeds']) == 14
    assert rss['unique_requests'] == len(stats['urls']) == len(set(stats['urls'])) == 13
    assert stats['maximum'] <= 4 and stats['closed']
    assert sum(row['status'] == 'unavailable' for row in rss['feeds']) == 1
    assert sum(row['status'] == 'ok' for row in rss['feeds']) == 13


async def test_default_catalog_offline_check_does_not_use_network(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, 'request', lambda *a, **kw: pytest.fail('오프라인 점검의 네트워크 호출'))
    config = Path(__file__).resolve().parents[1]/'config/sessionquant.yaml'
    result = await check_news(config)
    assert result['company_rss_catalog_enabled'] is True
    assert result['company_rss_catalog_scope'] == 'sp500_top100_market_cap'
    assert result['company_rss_issuer_count'] == len({row['cik'] for row in load_company_catalog()['companies'].values()})
    assert result['company_rss_count'] == sum(len(row['rss_urls']) for row in load_company_catalog()['companies'].values())
    assert result['connections'] == {} and result['llm_generation_calls'] == 0


def test_rss_catalog_scope_is_one_hundred_issuers_ranked_by_market_cap():
    """지수 비중 순서와 중복 클래스 합산으로 선정 범위를 바꾸지 않는다."""
    from decimal import Decimal
    import json
    from importlib.resources import files
    snapshot = json.loads(files('sessionquant').joinpath('config/rss_top100.json').read_text())
    selected = snapshot['companies']
    assert snapshot['scope'] == 'rss_only' and len(selected) == 100
    assert len({row['cik'] for row in selected}) == 100
    assert [row['rank'] for row in selected] == list(range(1, 101))
    caps = [Decimal(row['market_cap_usd']) for row in selected]
    assert caps == sorted(caps, reverse=True) and min(caps) > 0
    for row in selected:
        assert Decimal(row['market_cap_usd']) == max(Decimal(q['market_cap_usd']) for q in row['share_class_quotes'])
    allowed = {symbol for row in selected for symbol in row['symbols']}
    assert set(load_company_catalog()['companies']) <= allowed
    # 회사 RSS 범위는 뉴스 자료이며 감시 유니버스 경로·시세 소스 설정은 유지한다.
    config = SessionConfig()
    assert config.universe_path == Path('data/sp500-universe.json')
    assert config.source == 'toss'
