"""Read a bounded SEC primary document after official ticker/CIK validation."""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import os
import re
from urllib.parse import urlsplit

import httpx

from ..models import NewsEvent, symbol_valid
from .common import CapabilityUnavailable, DataQualityError
from .sec_edgar import Filing, SEC_LIMITER


class _DocumentText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class SecDocumentClient:
    def __init__(self, *, user_agent: str | None = None, client: httpx.AsyncClient | None = None,
                 max_bytes: int = 2_000_000):
        self.user_agent = user_agent if user_agent is not None else os.getenv("SEC_USER_AGENT", "")
        if not 1000 <= max_bytes <= 10_000_000:
            raise ValueError("invalid SEC document size limit")
        self.max_bytes = max_bytes
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self._owns_client = client is None

    async def fetch(self, filing: Filing, *, expected_symbol: str) -> NewsEvent:
        symbol_valid(expected_symbol)
        if expected_symbol not in filing.symbols:
            raise DataQualityError("SEC filing ticker does not match the candidate")
        if filing.published_at is None:
            raise DataQualityError("SEC filing has no verified acceptance timestamp")
        parsed = urlsplit(filing.url)
        prefix = f"/Archives/edgar/data/{int(filing.cik)}/{filing.accession.replace('-', '')}/"
        if (parsed.scheme != "https" or parsed.hostname != "www.sec.gov" or parsed.port not in {None, 443}
                or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not parsed.path.startswith(prefix)
                or ".." in parsed.path or "%2e" in parsed.path.lower()):
            raise DataQualityError("SEC document URL does not match its filing identity")
        if not self.user_agent or "@" not in self.user_agent or not self.user_agent.isascii():
            raise CapabilityUnavailable("Set SEC_USER_AGENT to an ASCII project name and contact email")
        await SEC_LIMITER.wait()
        body = bytearray()
        async with self.client.stream("GET", filing.url, headers={"User-Agent": self.user_agent}) as response:
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > self.max_bytes):
                raise DataQualityError("SEC document exceeds the allowed size")
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self.max_bytes:
                    raise DataQualityError("SEC document exceeds the allowed size")
        parser = _DocumentText()
        parser.feed(bytes(body).decode("utf-8", errors="replace"))
        text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
        if len(text) < 40:
            raise DataQualityError("SEC primary document has insufficient readable text")
        return NewsEvent("sec:" + filing.accession, filing.symbols,
                         f"SEC {filing.form} filing for {expected_symbol}", filing.published_at,
                         datetime.now(timezone.utc), "SEC EDGAR", filing.url, text,
                         verified=True, real_time=False)

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
