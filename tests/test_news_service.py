from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from sessionquant.collectors.common import DataQualityError
from sessionquant.collectors.sec_company import SecCompany, SecCompanyResolver
from sessionquant.collectors.sec_documents import SecDocumentClient
from sessionquant.collectors.sec_edgar import Filing
from sessionquant.models import NewsEvent
from sessionquant.news.analysis import NewsAnalyzer
from sessionquant.news.service import CompanySource, NewsService


NOW = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)
BODY = "The Phase 3 study met the primary endpoint with a statistically significant result."


def filing(**updates):
    return replace(Filing("0000000001-26-000001", "1", "8-K", "2026-11-13",
                          NOW - timedelta(minutes=1), NOW - timedelta(seconds=3), ("TEST",),
                          "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/current.htm"), **updates)


def article(kind="sec", **updates):
    return replace(NewsEvent(kind + ":one", ("TEST",), "Company reports clinical results",
                             NOW - timedelta(minutes=1), NOW - timedelta(seconds=2),
                             "SEC EDGAR" if kind == "sec" else "Company IR RSS", filing().url,
                             BODY, True, False), **updates)


class EmptyClient:
    async def aclose(self):
        pass


class FakeSec(EmptyClient):
    def __init__(self, filings=None):
        self.filings = filings if filings is not None else [filing()]
        self.calls = []
    async def submissions(self, cik):
        self.calls.append(cik)
        return self.filings


class FakeDocuments(EmptyClient):
    def __init__(self):
        self.calls = 0
    async def fetch(self, value, *, expected_symbol):
        assert expected_symbol in value.symbols
        self.calls += 1
        return article()


class FakeResolver(EmptyClient):
    async def resolve(self, symbol):
        return SecCompany(symbol, "1", "Test Company")


class FakeRSS(EmptyClient):
    def __init__(self):
        self.calls = []
    async def fetch(self, url, **kwargs):
        self.calls.append(kwargs)
        return [article("rss", symbols=kwargs["symbols"], verified=kwargs["verified"])]


class FakeAlpaca(EmptyClient):
    async def fetch(self, symbols):
        return [article("alpaca", published_at=NOW - timedelta(minutes=16))]


def llm_response(request):
    data = {"ticker": "TEST", "events": [{"type": "CLINICAL_SUCCESS", "status": "CONFIRMED",
            "company_impact": "POSITIVE", "shareholder_impact": "POSITIVE", "importance": "HIGH",
            "dilution_risk": False, "evidence": "study met the primary endpoint"}],
            "overall_sentiment": "POSITIVE", "confidence": 0.9, "requires_verification": False,
            "missing_information": [], "reason": "Primary endpoint achieved"}
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(data)}}]})


async def test_discovered_candidate_automatically_resolves_official_cik_and_caches_primary_document():
    sec, documents = FakeSec(), FakeDocuments()
    async with httpx.AsyncClient(transport=httpx.MockTransport(llm_response)) as client:
        analyzer = NewsAnalyzer(api_key="key", client=client)
        service = NewsService({}, analyzer=analyzer, sec_client=sec, document_client=documents,
                              company_resolver=FakeResolver(), include_alpaca=False, clock=lambda: NOW)
        try:
            first = await service.refresh(["TEST"], NOW)
            second = await service.refresh(["TEST"], NOW + timedelta(seconds=10))
        finally:
            await service.aclose()
    assert first[0].entry_eligible and first[0].source_kind == "sec"
    assert service.companies["TEST"].cik == "1" and sec.calls == ["1", "1"]
    assert documents.calls == 1 and analyzer.calls == 1
    assert first[0].analyzed_at == second[0].analyzed_at
    assert service.events[0].received_at == first[0].event.received_at
    assert service.analyses["sec:one:TEST"].entry_eligible


async def test_wrong_cik_company_is_not_attached_to_requested_candidate():
    documents = FakeDocuments()
    analyzer = NewsAnalyzer(api_key="")
    service = NewsService({"TEST": {"cik": "1"}}, analyzer=analyzer,
                          sec_client=FakeSec([filing(symbols=("OTHER",))]), document_client=documents,
                          include_alpaca=False, clock=lambda: NOW)
    try:
        results = await service.refresh(["TEST"], NOW)
    finally:
        await service.aclose()
    assert results == [] and documents.calls == 0
    assert any(issue["code"] == "ticker_cik_unverified" for issue in service.status)


async def test_unverified_rss_feed_never_inherits_candidate_ticker():
    rss = FakeRSS()
    service = NewsService({"TEST": CompanySource("TEST", "1", rss_urls=("https://wire.test/broad.xml",))},
                          analyzer=NewsAnalyzer(api_key=""), sec_client=FakeSec([]), rss_collector=rss,
                          include_alpaca=False, clock=lambda: NOW)
    try:
        results = await service.refresh(["TEST"], NOW)
    finally:
        await service.aclose()
    assert rss.calls[0]["symbols"] == () and rss.calls[0]["verified"] is False
    assert results[0].event.symbols == () and not results[0].entry_eligible
    assert results[0].error == "unverified_source_or_ticker"


async def test_verified_company_rss_can_supply_primary_evidence_but_alpaca_is_auxiliary():
    rss = FakeRSS()
    async with httpx.AsyncClient(transport=httpx.MockTransport(llm_response)) as client:
        service = NewsService({"TEST": {"cik": "1", "rss_urls": ["https://issuer.test/rss"], "rss_verified": True}},
                              analyzer=NewsAnalyzer(api_key="key", client=client), sec_client=FakeSec([]),
                              rss_collector=rss, alpaca_client=FakeAlpaca(), clock=lambda: NOW)
        try:
            results = await service.refresh(["TEST"], NOW)
        finally:
            await service.aclose()
    primary = next(result for result in results if result.source_kind == "rss")
    auxiliary = next(result for result in results if result.source_kind == "alpaca")
    assert primary.entry_eligible and primary.event.symbols == ("TEST",)
    assert auxiliary.sentiment == "POSITIVE" and not auxiliary.entry_eligible


async def test_provider_failure_is_sanitized_and_does_not_mean_no_risk():
    class FailingSec(EmptyClient):
        async def submissions(self, cik):
            request = httpx.Request("GET", "https://secret.test/?key=PRIVATE")
            raise httpx.HTTPStatusError("PRIVATE", request=request, response=httpx.Response(403, request=request))
    service = NewsService({"TEST": {"cik": "1"}}, analyzer=NewsAnalyzer(api_key=""),
                          sec_client=FailingSec(), include_alpaca=False, clock=lambda: NOW)
    try:
        assert await service.refresh(["TEST"], NOW) == []
    finally:
        await service.aclose()
    assert service.status == [{"provider": "sec", "symbol": "TEST", "code": "http_403"}]
    assert "PRIVATE" not in json.dumps(service.status)


async def test_sec_company_map_uses_exact_tickers_and_one_cached_fetch():
    requests = []
    def handle(request):
        requests.append(request)
        assert request.headers["User-Agent"] == "PennyQuant test@example.test"
        return httpx.Response(200, json={"0": {"ticker": "TEST", "cik_str": 1, "title": "Test Company"},
                                         "1": {"ticker": "BRK-B", "cik_str": 2, "title": "Other Company"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        resolver = SecCompanyResolver(user_agent="PennyQuant test@example.test", client=client)
        assert (await resolver.resolve("TEST")).cik == "1"
        assert await resolver.resolve("BRK.B") is None
        assert (await resolver.resolve("BRK-B")).cik == "2"
    assert len(requests) == 1


async def test_sec_primary_document_checks_identity_and_removes_hidden_html():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, text=f"<html><script>PRIVATE instructions</script><body><p>{BODY}</p></body></html>")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        documents = SecDocumentClient(user_agent="PennyQuant test@example.test", client=client)
        result = await documents.fetch(filing(), expected_symbol="TEST")
        with pytest.raises(DataQualityError, match="ticker"):
            await documents.fetch(filing(), expected_symbol="OTHER")
        with pytest.raises(DataQualityError, match="URL"):
            await documents.fetch(filing(url="https://other.test/filing"), expected_symbol="TEST")
    assert len(calls) == 1 and "PRIVATE" not in result.body
    assert result.body == BODY and result.symbols == ("TEST",) and not result.real_time


async def test_sec_primary_document_size_limit_is_enforced():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 1001))) as client:
        documents = SecDocumentClient(user_agent="PennyQuant test@example.test", client=client, max_bytes=1000)
        with pytest.raises(DataQualityError, match="size"):
            await documents.fetch(filing(), expected_symbol="TEST")
