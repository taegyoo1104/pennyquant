"""SEC submissions metadata. Filing form alone never supplies sentiment."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import re
from urllib.parse import quote

import httpx

from ..models import NewsEvent, utc
from .common import CapabilityUnavailable, DataQualityError, RateLimiter, get_readonly, now_utc

WATCHED_FORMS = {"8-K", "6-K", "S-3", "424B5", "10-Q", "10-K"}
# One shared in-process limiter, including multiple clients and retries. Run only
# one collector process per user; cross-machine limits require external coordination.
SEC_LIMITER = RateLimiter(5)


@dataclass(frozen=True)
class Filing:
    accession: str
    cik: str
    form: str
    filing_date: str
    published_at: datetime | None
    received_at: datetime
    symbols: tuple[str, ...]
    url: str

    def as_news(self) -> NewsEvent | None:
        if self.published_at is None:
            return None  # filingDate is date-only; never fabricate midnight news.
        return NewsEvent("sec:" + self.accession, self.symbols,
            f"SEC {self.form} filing", self.published_at, self.received_at,
            "SEC EDGAR", self.url, "Filing metadata only; full filing content requires separate review.",
            verified=True, real_time=False)


def parse_submissions(data: dict, cik: str, received_at: datetime) -> list[Filing]:
    recent = data.get("filings", {}).get("recent", {})
    symbols = tuple(data.get("tickers", []))
    result = []
    forms = recent.get("form", [])
    for index, form in enumerate(forms):
        if form.removesuffix("/A") not in WATCHED_FORMS:
            continue
        def item(key, default=""):
            values = recent.get(key, [])
            return values[index] if index < len(values) else default
        accession, primary = item("accessionNumber"), item("primaryDocument")
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            continue
        published = None
        try:
            published = utc(item("acceptanceDateTime"))
        except (ValueError, TypeError):
            pass
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{quote(primary, safe='')}"
        result.append(Filing(accession, cik, form, item("filingDate"), published,
                             utc(received_at), symbols, url))
    return result


class SecEdgarClient:
    def __init__(self, user_agent: str | None = None, *, client: httpx.AsyncClient | None = None):
        self.user_agent = user_agent or os.getenv("SEC_USER_AGENT", "")
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self._owns_client = client is None

    async def submissions(self, cik: str | int) -> list[Filing]:
        cik = str(cik)
        if not cik.isdigit() or not 0 < int(cik) < 10**10:
            raise ValueError("CIK must contain 1..10 digits")
        if not self.user_agent or "@" not in self.user_agent:
            raise CapabilityUnavailable("Set SEC_USER_AGENT to your project/company name and contact email")
        cik = cik.zfill(10)
        response = await get_readonly(self.client, f"https://data.sec.gov/submissions/CIK{cik}.json",
            limiter=SEC_LIMITER, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        data = response.json()
        if int(data.get("cik", cik)) != int(cik):
            raise DataQualityError("SEC response CIK does not match request")
        return parse_submissions(data, cik, now_utc())

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
