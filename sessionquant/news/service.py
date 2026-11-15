"""Candidate-scoped SEC/RSS evidence with delayed Alpaca as auxiliary input."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.parse import urlsplit

import httpx

from ..collectors.alpaca_news import AlpacaNewsClient
from ..collectors.sec_company import SecCompanyResolver
from ..collectors.rss_collector import RSSCollector
from ..collectors.sec_documents import SecDocumentClient
from ..collectors.sec_edgar import SecEdgarClient
from ..models import NewsEvent, symbol_valid, utc
from .analysis import NewsAnalysis, NewsAnalyzer, event_digest


@dataclass(frozen=True)
class CompanySource:
    symbol: str
    cik: str = ""
    company_name: str = ""
    rss_urls: tuple[str, ...] = ()
    rss_verified: bool = False

    def __post_init__(self):
        symbol_valid(self.symbol)
        if self.cik and (not str(self.cik).isdigit() or not 0 < int(self.cik) < 10**10):
            raise ValueError("Company CIK must be a valid SEC identifier")
        if type(self.rss_verified) is not bool or not isinstance(self.company_name, str):
            raise ValueError("invalid company source metadata")
        if not isinstance(self.rss_urls, (tuple, list)) or len(self.rss_urls) > 10:
            raise ValueError("company RSS feeds must be a bounded list")
        for url in self.rss_urls:
            if not isinstance(url, str):
                raise ValueError("company RSS feed must be an HTTPS URL")
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("company RSS feed must be an HTTPS URL without embedded credentials")


class NewsService:
    def __init__(self, companies: Mapping[str, CompanySource | dict], *, analyzer: NewsAnalyzer | None = None,
                 sec_client=None, document_client=None, rss_collector=None, alpaca_client=None,
                 include_alpaca: bool = True, max_filings_per_symbol: int = 3,
                 clock: Callable[[], datetime] | None = None, company_resolver=None, repository=None):
        if type(max_filings_per_symbol) is not int or not 1 <= max_filings_per_symbol <= 10:
            raise ValueError("invalid per-company filing limit")
        self.repo=repository
        from .schedule import NewsSchedule
        self.schedule=NewsSchedule(repository,clock=clock)
        self.companies = {}
        for symbol, value in companies.items():
            symbol_valid(symbol)
            if isinstance(value, CompanySource):
                source = value
            elif isinstance(value, dict):
                source = CompanySource(symbol=symbol, cik=str(value.get("cik") or ""),
                                       company_name=value.get("company_name", ""),
                                       rss_urls=tuple(value.get("rss_urls", ())),
                                       rss_verified=value.get("rss_verified", False))
            else:
                raise ValueError("company sources must contain explicit mappings")
            if source.symbol != symbol:
                raise ValueError("company source symbol does not match its mapping key")
            self.companies[symbol] = source
        self.analyzer = analyzer or NewsAnalyzer()
        self.sec_client = sec_client or SecEdgarClient()
        self.document_client = document_client or SecDocumentClient()
        self.rss_collector = rss_collector or RSSCollector(repository=repository,schedule=self.schedule)
        self.alpaca_client = alpaca_client or AlpacaNewsClient()
        self.company_resolver = company_resolver or SecCompanyResolver()
        self.include_alpaca, self.max_filings_per_symbol = include_alpaca, max_filings_per_symbol
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.status: list[dict] = []
        self._events: dict[str, NewsEvent] = {}
        self._documents: dict[str, NewsEvent] = {}
        self.analyses: dict[str, NewsAnalysis] = {}

    @property
    def events(self) -> list[NewsEvent]:
        return list(self._events.values())

    def _issue(self, provider: str, symbol: str, exc=None, *, code: str = ""):
        if not code:
            code = f"http_{exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
        self.status.append({"provider": provider, "symbol": symbol, "code": code})

    async def _analyze(self, event: NewsEvent, symbol: str, kind: str, primary: bool, now: datetime):
        original = self._events.get(event.id)
        if original is not None and event_digest(original, symbol) == event_digest(event, symbol):
            event = original
        self._events[event.id] = event
        from .intake import company_reason,archive
        analyzed_at = max(now, utc(self.clock()))
        reason=company_reason(event,symbol,kind,primary,analyzed_at,getattr(self.analyzer,'max_age_hours',24))
        if self.repo:
            from ..models import jsonable
            archive(self.repo,jsonable(event),provider=kind,reason=reason,ticker=symbol)
        if reason:
            result = NewsAnalysis(symbol, event, source_kind=kind, analyzed_at=analyzed_at,
                error=reason, reason='뉴스의 출처·종목·시각·본문 확인이 필요합니다.',
                input_digest=event_digest(event, symbol))
        else:
            result = await self.analyzer.analyze(event, symbol, source_kind=kind, primary_verified=primary,
                                                 now=analyzed_at)
        self.analyses[f"{event.id}:{symbol}"] = result
        return result

    async def _request(self,name,call):
        if not self.schedule.claim(name,0):return []
        try:
            result=await call()
            self.schedule.success(name,len(result) if hasattr(result,'__len__') else int(result is not None))
            return result
        except (httpx.HTTPError,RuntimeError,ValueError,TypeError,KeyError) as exc:
            self.schedule.failure(name,exc);raise

    async def refresh(self, symbols: list[str], now: datetime | None = None) -> list[NewsAnalysis]:
        now = utc(now or self.clock())
        symbols = list(dict.fromkeys(symbols))
        if len(symbols) > 200:
            raise ValueError("too many news candidates")
        for symbol in symbols:
            symbol_valid(symbol)
        self.status = []
        results = []
        for symbol in symbols:
            source = self.companies.get(symbol)
            if source is None or not source.cik:
                try:
                    company = await self._request('sec_company',lambda:self.company_resolver.resolve(symbol))
                    if company:
                        source = CompanySource(symbol, company.cik, company.company_name,
                                               source.rss_urls if source else (),
                                               source.rss_verified if source else False)
                        self.companies[symbol] = source
                except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                    self._issue("sec_company", symbol, exc)
            if source is None:
                self._issue("primary", symbol, code="company_mapping_missing")
                continue
            if source.cik:
                try:
                    filings = await self._request('sec:'+symbol,lambda:self.sec_client.submissions(source.cik))
                    # This symbol association comes from SEC's submissions response,
                    # never from the candidate/query or model's interpretation.
                    matching = [filing for filing in filings if symbol in filing.symbols
                                and filing.published_at is not None and utc(filing.published_at) <= now
                                and (now - utc(filing.published_at)).total_seconds() <= self.analyzer.max_age_hours * 3600]
                    if not filings:
                        self._issue("sec", symbol, code="primary_filing_unavailable")
                    elif not any(symbol in filing.symbols for filing in filings):
                        self._issue("sec", symbol, code="ticker_cik_unverified")
                    for filing in sorted(matching, key=lambda item: utc(item.published_at), reverse=True)[:self.max_filings_per_symbol]:
                        try:
                            if filing.accession not in self._documents:
                                self._documents[filing.accession] = await self.document_client.fetch(filing, expected_symbol=symbol)
                            event = self._documents[filing.accession]
                            results.append(await self._analyze(event, symbol, "sec", True, now))
                        except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError):
                            self._issue("sec_document", symbol, code="document_unavailable")
                except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                    self._issue("sec", symbol, exc)
            for url in source.rss_urls:
                try:
                    events = await self.rss_collector.fetch(url, source=f"Company IR RSS:{symbol}",
                                                            symbols=(symbol,) if source.rss_verified else (),
                                                            verified=source.rss_verified)
                    if not source.rss_verified:
                        self._issue("rss", symbol, code="company_feed_mapping_unverified")
                    for event in events[:20]:
                        results.append(await self._analyze(event, symbol, "rss", source.rss_verified, now))
                except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                    self._issue("rss", symbol, exc)
        if self.include_alpaca and symbols:
            try:
                from datetime import timedelta
                import inspect
                options={'start':now-timedelta(hours=24),'end':now-timedelta(minutes=15),'max_pages':2,'skip_invalid':True}
                parameters=inspect.signature(self.alpaca_client.fetch).parameters
                options={key:value for key,value in options.items() if key in parameters or any(p.kind==p.VAR_KEYWORD for p in parameters.values())}
                events = await self._request('alpaca',lambda:self.alpaca_client.fetch(symbols,**options))
                if self.repo:
                    from .intake import archive
                    for item in getattr(self.alpaca_client,'intake_errors',[]):
                        archive(self.repo,{'id':str(item['raw'].get('id','unknown')),'received_at':item['received_at'],**item['raw']},provider='alpaca',reason=item['skip_reason'])
                for event in events[:50]:
                    self._events[event.id] = event
                    for symbol in symbols:
                        if symbol in event.symbols:
                            results.append(await self._analyze(event, symbol, "alpaca", False, now))
            except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                self._issue("alpaca", "", exc)
        # Bound memory without changing first availability of cached SEC documents.
        while len(self._events) > 5000:
            self._events.pop(next(iter(self._events)))
        while len(self._documents) > 1000:
            self._documents.pop(next(iter(self._documents)))
        while len(self.analyses) > 10000:
            self.analyses.pop(next(iter(self.analyses)))
        return results

    async def aclose(self):
        for client in (self.analyzer, self.sec_client, self.document_client, self.rss_collector,
                       self.alpaca_client, self.company_resolver):
            await client.aclose()
