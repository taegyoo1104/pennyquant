"""SEC's exact ticker-to-CIK map; no company-name or punctuation guessing."""
from __future__ import annotations

from dataclasses import dataclass
import os
from time import monotonic

import httpx

from ..models import symbol_valid
from .common import CapabilityUnavailable, DataQualityError, get_readonly
from .sec_edgar import SEC_LIMITER


@dataclass(frozen=True)
class SecCompany:
    symbol: str
    cik: str
    company_name: str


class SecCompanyResolver:
    URL = "https://www.sec.gov/files/company_tickers.json"

    def __init__(self, *, user_agent: str | None = None, client: httpx.AsyncClient | None = None,
                 cache_seconds: float = 86400):
        self.user_agent = user_agent if user_agent is not None else os.getenv("SEC_USER_AGENT", "")
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self._owns_client = client is None
        if not 60 <= cache_seconds <= 86400:
            raise ValueError("invalid SEC company-map cache duration")
        self.cache_seconds = cache_seconds
        self._loaded_at: float | None = None
        self._companies: dict[str, SecCompany] = {}

    async def resolve(self, symbol: str) -> SecCompany | None:
        symbol_valid(symbol)
        if self._loaded_at is None or monotonic() - self._loaded_at >= self.cache_seconds:
            if not self.user_agent or "@" not in self.user_agent or not self.user_agent.isascii():
                raise CapabilityUnavailable("Set SEC_USER_AGENT to an ASCII project name and contact email")
            response = await get_readonly(self.client, self.URL, limiter=SEC_LIMITER,
                                          headers={"User-Agent": self.user_agent})
            if len(response.content) > 6_000_000:
                raise DataQualityError("SEC company map exceeds the allowed size")
            data = response.json()
            if not isinstance(data, dict) or not 1 <= len(data) <= 50000:
                raise DataQualityError("SEC company map has an invalid structure")
            companies = {}
            for value in data.values():
                if not isinstance(value, dict):
                    continue
                ticker, cik, name = value.get("ticker"), value.get("cik_str"), value.get("title")
                try:
                    symbol_valid(ticker)
                except ValueError:
                    continue
                if type(cik) is not int or not 0 < cik < 10**10 or not isinstance(name, str) or not name.strip():
                    continue
                record = SecCompany(ticker, str(cik), name)
                if ticker in companies and companies[ticker] != record:
                    raise DataQualityError("SEC company map contains an ambiguous ticker")
                companies[ticker] = record
            if not companies:
                raise DataQualityError("SEC company map contains no valid records")
            self._companies, self._loaded_at = companies, monotonic()
        return self._companies.get(symbol)

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
