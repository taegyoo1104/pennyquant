"""발행 시각과 출처를 보존하는 RSS·Atom 수집기. 종목은 추측하지 않는다."""
from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import httpx

from ..models import NewsEvent, utc
from .common import DataQualityError, RateLimiter, get_readonly, now_utc


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(text: str) -> str:
    parser = _PlainText()
    parser.feed(text)
    return " ".join(" ".join(parser.parts).split())


def _url_identity(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
                      urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                                 if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}]), ""))


def parse_feed(xml: bytes, *, source: str, received_at: datetime,
               symbols: tuple[str, ...] = (), verified: bool = False) -> list[NewsEvent]:
    """기업 전용 피드와 종목 연결을 확인한 호출자만 symbols와 verified를 지정한다.

    updated는 수정 시각이므로 발행 시각을 대체하지 않는다. 짧거나 오래된 본문도
    임의로 보완하지 않으며, 광역 피드에는 symbols=()를 사용한다.
    """
    if len(xml) > 4_000_000 or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise DataQualityError("Feed too large or contains forbidden XML declarations")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise DataQualityError("RSS/Atom XML 형식을 해석할 수 없습니다") from None
    atom = "{http://www.w3.org/2005/Atom}"
    content = "{http://purl.org/rss/1.0/modules/content/}encoded"
    if root.tag not in {"rss", atom + "feed"}:
        raise DataQualityError("RSS 또는 Atom 피드가 아닌 응답입니다")
    entries = root.findall("./channel/item") + root.findall(atom + "entry")
    result, seen = [], set()
    for entry in entries:
        def value(tag, default=""):
            node = entry.find(tag)
            return " ".join(node.itertext()).strip() if node is not None else default
        is_atom = entry.tag == atom + "entry"
        title = plain_text(value(atom + "title" if is_atom else "title"))
        if is_atom:
            links = entry.findall(atom + "link")
            link = next((n.get("href", "") for n in links if n.get("rel", "alternate") == "alternate"), "")
            date_text = value(atom + "published")
            guid = value(atom + "id")
            body = value(atom + "content") or value(atom + "summary")
        else:
            link, date_text = value("link"), value("pubDate")
            guid, body = value("guid"), value(content) or value("description")
        try:
            published = utc(date_text) if is_atom else utc(parsedate_to_datetime(date_text))
        except (ValueError, TypeError, AttributeError):
            continue
        if published > utc(received_at):
            continue
        identity = _url_identity(link) if link else guid
        if not title or not identity:
            continue
        event_id = "rss:" + sha256((source + "|" + identity).encode()).hexdigest()[:32]
        if event_id in seen:
            continue
        seen.add(event_id)
        result.append(NewsEvent(event_id, symbols, title, published, utc(received_at),
                                source, link, plain_text(unescape(body)), verified=verified, real_time=False))
    return sorted(result, key=lambda event: event.published_at, reverse=True)


class RSSCollector:
    def __init__(self, *, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._owns_client = client is None
        self.limiter = RateLimiter(1)
        self._host_limiters = {}

    async def fetch(self, url: str, *, source: str, symbols: tuple[str, ...] = (),
                    verified: bool = False) -> list[NewsEvent]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("인증정보 없는 HTTPS 피드 URL이 필요합니다")
        limiter = self._host_limiters.setdefault(parsed.hostname, RateLimiter(1))
        response = await get_readonly(self.client, url, limiter=limiter,
                                      headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.1"})
        return parse_feed(response.content, source=source, received_at=now_utc(),
                          symbols=symbols, verified=verified)

    async def enrich(self, event: NewsEvent, *, allowed_hosts: tuple[str, ...], max_chars: int = 20000) -> NewsEvent:
        """관리자가 확인한 공식 호스트의 공개 본문만 보완한다. 리디렉션은 따라가지 않는다."""
        from dataclasses import replace
        parsed = urlsplit(event.url)
        host = parsed.hostname or ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise DataQualityError("기사 보완 조회에는 IP 주소를 사용할 수 없습니다")
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
                or host not in allowed_hosts or host in {"localhost", "localhost.localdomain"}
                or host.endswith((".local", ".internal"))):
            raise DataQualityError("공식 본문 보완 조회 호스트가 확인되지 않았습니다")
        limiter = self._host_limiters.setdefault(host, RateLimiter(1))
        response = await get_readonly(self.client, event.url, limiter=limiter)
        if not 200 <= response.status_code < 300 or len(response.content) > 2_000_000:
            raise DataQualityError("기사 본문 크기 또는 응답 상태를 확인하십시오")
        body = plain_text(response.text)[:max_chars]
        return replace(event, body=body) if len(body) > len(event.body) else event

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
