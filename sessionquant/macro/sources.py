"""공식 기관 RSS, GDELT 기사 탐색, FRED 일별 보조자료 수집."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from urllib.parse import urlsplit

import httpx

from ..collectors.common import CapabilityUnavailable, DataQualityError, RateLimiter, get_readonly
from ..collectors.rss_collector import RSSCollector
from ..models import utc
from .models import MacroArticle, TOPICS

OFFICIAL_FEEDS = {
    "fed": ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml", "us_monetary_policy"),
    "bls": ("BLS", "https://www.bls.gov/feed/bls_latest.rss", "inflation_and_employment"),
    "bea": ("BEA", "https://apps.bea.gov/rss/rss.xml", "inflation_and_employment"),
    "eia": ("EIA", "https://www.eia.gov/rss/press_rss.xml", "oil_supply_disruption"),
}
OFFICIAL_DOMAINS = {"federalreserve.gov", "bls.gov", "bea.gov", "treasury.gov", "eia.gov"}


def official_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and not parsed.username and not parsed.password and any(
        host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS)


class OfficialNewsClient:
    def __init__(self, *, rss=None, feeds: dict | None = None, enrich: bool = False):
        self.rss = rss or RSSCollector()
        self.feeds = feeds if feeds is not None else OFFICIAL_FEEDS
        self.enrich = enrich
        self.status = []

    async def fetch(self) -> list[MacroArticle]:
        self.status, articles = [], []
        for key, (name, url, topic) in self.feeds.items():
            if not official_url(url):
                self.status.append({"provider": key, "code": "unverified_official_feed"})
                continue
            try:
                events = await self.rss.fetch(url, source=name, verified=True)
                for event in events[:20]:
                    if not official_url(event.url):
                        continue
                    if self.enrich and len(event.body) < 200:
                        try:
                            event = await self.rss.enrich(event, allowed_hosts=(urlsplit(event.url).hostname,))
                        except (httpx.HTTPError, ValueError, RuntimeError):
                            self.status.append({"provider": key, "code": "body_unavailable"})
                    articles.append(MacroArticle(event.url, event.title, event.body, name,
                                                 event.published_at, event.received_at, topic, True,
                                                 source_family=key))
                self.status.append({"provider": key, "code": "ok", "count": len(events)})
            except (httpx.HTTPError, ValueError, RuntimeError, TypeError):
                self.status.append({"provider": key, "code": "feed_unavailable"})
        return articles

    async def aclose(self):
        await self.rss.aclose()


class GDELTClient:
    def __init__(self, *, client: httpx.AsyncClient | None = None, topics: tuple[str, ...] | None = None):
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self._owns_client = client is None
        self.topics = topics if topics is not None else tuple(TOPICS)
        if any(topic not in TOPICS for topic in self.topics):
            raise ValueError("GDELT 주제 이름을 확인하십시오")
        self.limiter = RateLimiter(0.2)
        self.status = []

    async def fetch(self, *, now: datetime | None = None) -> list[MacroArticle]:
        now = utc(now or datetime.now(timezone.utc))
        self.status, articles, seen = [], [], set()
        for topic in self.topics:
            try:
                response = await get_readonly(self.client, "https://api.gdeltproject.org/api/v2/doc/doc",
                    limiter=self.limiter, attempts=2, params={"query": TOPICS[topic], "mode": "artlist",
                    "format": "json", "maxrecords": 25, "timespan": "1h", "sort": "datedesc"})
                if len(response.content) > 2_000_000:
                    raise DataQualityError("GDELT 응답 크기 초과")
                for item in response.json().get("articles", [])[:25]:
                    article = MacroArticle(str(item["url"]), str(item["title"]), "",
                        str(item.get("domain", "")), None, now, topic, False,
                        str(item.get("language", "unknown")), "gdelt", str(item.get("domain", "")))
                    # seendate는 GDELT 발견 시각으로 실제 공개 시각을 대신하지 않는다.
                    if article.id not in seen:
                        articles.append(article)
                        seen.add(article.id)
                self.status.append({"provider": "gdelt", "topic": topic, "code": "ok"})
            except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError):
                self.status.append({"provider": "gdelt", "topic": topic, "code": "discovery_unavailable"})
        return articles

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()


class FREDClient:
    SERIES = ("DGS2", "DGS10", "DCOILWTICO")

    def __init__(self, *, api_key: str | None = None, client: httpx.AsyncClient | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("FRED_API_KEY", "")
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._owns_client = client is None
        self.limiter = RateLimiter(1)

    async def fetch(self, *, now: datetime | None = None) -> list[dict]:
        if not self.api_key:
            raise CapabilityUnavailable("FRED_API_KEY 설정이 필요합니다. FRED 자료는 일별 참고 데이터입니다")
        now = utc(now or datetime.now(timezone.utc))
        values = []
        for series in self.SERIES:
            response = await get_readonly(self.client, "https://api.stlouisfed.org/fred/series/observations",
                limiter=self.limiter, params={"api_key": self.api_key, "series_id": series, "file_type": "json",
                                             "sort_order": "desc", "limit": 10,
                                             "realtime_start": now.date().isoformat(), "realtime_end": now.date().isoformat()})
            for item in response.json().get("observations", []):
                if item.get("value") == "." or item.get("date", "9999") > now.date().isoformat():
                    continue
                from decimal import Decimal, InvalidOperation
                try:
                    value = Decimal(item["value"])
                    if not value.is_finite():
                        continue
                except (InvalidOperation, KeyError, TypeError):
                    continue
                values.append({"id": f"fred:{series}:{item['date']}:{now.date()}", "series": series,
                    "observation_date": item["date"], "received_at": now.isoformat(), "value": str(value),
                    "units": "percent" if series in {"DGS2", "DGS10"} else "usd_per_barrel",
                    "frequency": "daily", "real_time": False, "auxiliary_only": True,
                    "realtime_start": item.get("realtime_start"), "realtime_end": item.get("realtime_end")})
                break
        return values

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()


class TreasuryClient:
    """재무부 공식 HTML에서 발표를 찾는다. 확인할 수 없는 공개 시각은 비워 둔다."""
    URL = 'https://home.treasury.gov/news/press-releases'

    def __init__(self, *, client=None):
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._owns_client = client is None
        self.limiter = RateLimiter(1)
        self.status = []

    async def fetch(self):
        from html.parser import HTMLParser
        from urllib.parse import urljoin
        class Links(HTMLParser):
            def __init__(self):
                super().__init__(); self.links=[]; self.href=None; self.parts=[]
            def handle_starttag(self,tag,attrs):
                if tag=='a': self.href=dict(attrs).get('href'); self.parts=[]
            def handle_data(self,data):
                if self.href: self.parts.append(data)
            def handle_endtag(self,tag):
                if tag=='a' and self.href:
                    self.links.append((self.href,' '.join(' '.join(self.parts).split())))
                    self.href=None
        response = await get_readonly(self.client,self.URL,limiter=self.limiter,attempts=2)
        if len(response.content)>2_000_000: raise DataQualityError('재무부 응답 크기 초과')
        parser=Links(); parser.feed(response.text)
        articles=[]; seen=set(); now=datetime.now(timezone.utc)
        for href,title in parser.links:
            url=urljoin(self.URL,href)
            if (not title or len(title)<12 or not official_url(url) or urlsplit(url).hostname!='home.treasury.gov'
                    or not urlsplit(url).path.startswith('/news/press-releases/') or url in seen): continue
            seen.add(url)
            articles.append(MacroArticle(url,title,'','U.S. Treasury',None,now,
                                         'financial_system_risk',True,source_family='treasury',discovery_source='official_html'))
            if len(articles)>=10: break
        self.status=[{'provider':'treasury','code':'publication_time_unknown','count':len(articles)}]
        return articles

    async def aclose(self):
        if self._owns_client: await self.client.aclose()
