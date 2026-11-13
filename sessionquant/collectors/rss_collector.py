"""Conservative RSS/Atom ingestion: no guessed ticker or publication time."""
from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx

from ..models import NewsEvent, utc
from .common import DataQualityError, RateLimiter, get_readonly, now_utc


def parse_feed(xml: bytes, *, source: str, received_at: datetime,
               symbols: tuple[str, ...] = (), verified: bool = False) -> list[NewsEvent]:
    """symbols must be a verified company-specific feed mapping supplied by caller.

    Broad feeds should use symbols=() until a separate verified identity match.
    RSS is always auxiliary real_time=False.
    """
    if len(xml) > 4_000_000 or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise DataQualityError("Feed too large or contains forbidden XML declarations")
    root = ET.fromstring(xml)
    atom = "{http://www.w3.org/2005/Atom}"
    entries = root.findall("./channel/item") + root.findall(atom + "entry")
    result, seen = [], set()
    for entry in entries:
        def value(tag, default=""):
            node = entry.find(tag)
            return (node.text or default).strip() if node is not None else default
        is_atom = entry.tag == atom + "entry"
        title = value(atom + "title" if is_atom else "title")
        if is_atom:
            links = entry.findall(atom + "link")
            link = next((n.get("href", "") for n in links if n.get("rel", "alternate") == "alternate"), "")
            # updated is a revision time and must not freshen an old catalyst.
            date_text = value(atom + "published")
            identity = value(atom + "id") or link
            body = value(atom + "summary") or value(atom + "content")
        else:
            link, date_text = value("link"), value("pubDate")
            identity, body = value("guid") or link, value("description")
        try:
            published = utc(date_text) if is_atom else utc(parsedate_to_datetime(date_text))
        except (ValueError, TypeError, AttributeError):
            continue
        if not title or not identity:
            continue
        event_id = "rss:" + sha256((source + "|" + identity).encode()).hexdigest()[:32]
        if event_id in seen:
            continue
        seen.add(event_id)
        result.append(NewsEvent(event_id, symbols, title, published, utc(received_at),
                                source, link, body, verified=verified, real_time=False))
    return result


class RSSCollector:
    def __init__(self, *, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._owns_client = client is None
        self.limiter = RateLimiter(1)

    async def fetch(self, url: str, *, source: str, symbols: tuple[str, ...] = (),
                    verified: bool = False) -> list[NewsEvent]:
        if urlsplit(url).scheme != "https":
            raise ValueError("HTTPS feed URL required")
        response = await get_readonly(self.client, url, limiter=self.limiter,
                                      headers={"Accept": "application/rss+xml, application/atom+xml, application/xml"})
        return parse_feed(response.content, source=source, received_at=now_utc(),
                          symbols=symbols, verified=verified)

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
