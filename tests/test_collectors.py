import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from pennyquant.collectors.alpaca_news import AlpacaNewsClient, parse_news
from pennyquant.collectors.common import CapabilityUnavailable, DataQualityError, RateLimiter, get_readonly
from pennyquant.collectors.rss_collector import parse_feed
from pennyquant.collectors.sec_edgar import SecEdgarClient, parse_submissions
from pennyquant.collectors.toss_market import TossMarketClient, parse_candles, parse_orderbook
from pennyquant.collectors.toss_websocket import TossWebSocket, quote_from_frame, subscription_declaration

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)


def orderbook():
    return {"currency": "USD", "timestamp": "2026-11-13T15:00:00Z",
            "asks": [{"price": "2.02", "volume": "100"}, {"price": "2.01", "volume": "20"}],
            "bids": [{"price": "2.00", "volume": "30"}]}


async def test_toss_auth_encoding_token_cache_account_header_and_no_live_orders():
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path == "/oauth2/token":
            assert parse_qs(request.content.decode()) == {"grant_type": ["client_credentials"],
                                                        "client_id": ["test-id"], "client_secret": ["a+b&c"]}
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 86400})
        assert request.headers["Authorization"] == "Bearer fake-token"
        if request.url.path == "/api/v1/holdings":
            assert request.headers["X-Tossinvest-Account"] == "3"
            return httpx.Response(200, json={"result": {"items": []}})
        assert "X-Tossinvest-Account" not in request.headers
        return httpx.Response(200, json={"result": [{"symbol": "TEST"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        toss = TossMarketClient("test-id", "a+b&c", 3, client=client)
        toss.limiter.interval = 0
        assert await toss.prices(["TEST"]) == [{"symbol": "TEST"}]
        assert await toss.holdings() == {"items": []}
        with pytest.raises(CapabilityUnavailable, match="disabled"):
            await toss.create_order()
    assert [r.method for r in requests] == ["POST", "GET", "GET"]


async def test_toss_token_post_not_retried_and_401_not_renewed():
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "fake", "expires_in": 86400})
        return httpx.Response(401, json={"error": {"code": "invalid-token"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        toss = TossMarketClient("id", "secret", client=client)
        toss.limiter.interval = 0
        with pytest.raises(httpx.HTTPStatusError):
            await toss.prices(["TEST"])
    assert calls == ["/oauth2/token", "/api/v1/prices"]


def test_quote_sorts_levels_and_does_not_invent_eligibility_or_time():
    quote = parse_orderbook("TEST", orderbook(), NOW)
    assert quote.ask == 2.01 and quote.ask_size == 20 and quote.bid_size == 30
    assert quote.exchange == "UNKNOWN" and not quote.tradable
    with pytest.raises(DataQualityError, match="known event time"):
        parse_orderbook("TEST", {**orderbook(), "timestamp": None}, NOW)
    with pytest.raises(DataQualityError, match="two-sided"):
        parse_orderbook("TEST", {**orderbook(), "bids": []}, NOW)


def test_candles_end_timestamp_sort_and_exclude_future():
    def candle(timestamp):
        return {"currency": "USD", "timestamp": timestamp, "openPrice": "2", "highPrice": "2.1",
                "lowPrice": "1.9", "closePrice": "2.01", "volume": "100"}
    bars = parse_candles("TEST", {"candles": [candle("2026-11-13T15:01:00Z"),
        candle("2026-11-13T15:00:00Z"), candle("2026-11-13T14:59:00Z")]}, NOW)
    assert len(bars) == 2 and bars[0].timestamp.minute == 59 and bars[-1].timestamp == NOW


def test_subscription_counts_channels_and_frames_are_not_mock_quotes():
    declaration = subscription_declaration(["TEST", "OTHER", "TEST"], account_seq=3)
    assert sum(len(d["codes"]) for d in declaration) == 5
    assert declaration[-1] == {"type": "personal:order", "codes": ["3"]}
    with pytest.raises(ValueError, match="candidate limit"):
        subscription_declaration([f"T{i}" for i in range(21)])
    with pytest.raises(ValueError, match="100"):
        subscription_declaration([f"T{i}" for i in range(51)], max_candidates=100)
    assert quote_from_frame({"type": "pong"}) is None
    quote = quote_from_frame({"type": "message", "topic": "orderbook:us:TEST", "data": orderbook()}, received_at=NOW)
    assert quote.symbol == "TEST" and not quote.tradable


async def test_retry_honors_readonly_429_and_bounds_attempts(monkeypatch):
    waits, calls = [], []
    async def sleep(delay):
        waits.append(delay)
    monkeypatch.setattr("pennyquant.collectors.common.asyncio.sleep", sleep)
    def handle(request):
        calls.append(request.method)
        return httpx.Response(429, headers={"Retry-After": "0.7"}) if len(calls) < 3 else httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await get_readonly(client, "https://example.test/news", limiter=RateLimiter(5))
    assert calls == ["GET", "GET", "GET"] and waits.count(0.7) == 2


async def test_alpaca_rest_paginates_deduplicates_but_is_never_marked_live(monkeypatch):
    monkeypatch.setattr("pennyquant.collectors.alpaca_news.now_utc", lambda: NOW)
    article = {"id": 1, "headline": "FDA approval", "symbols": ["TEST"],
               "created_at": "2026-11-13T14:00:00Z", "source": "verified-wire"}
    def handle(request):
        assert request.headers["APCA-API-KEY-ID"] == "key"
        page = request.url.params.get("page_token")
        return httpx.Response(200, json={"news": [article], "next_page_token": None if page else "page2"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        alpaca = AlpacaNewsClient("key", "secret", realtime_verified=True, client=client)
        alpaca.limiter.interval = 0
        news = await alpaca.fetch(["TEST"])
    assert len(news) == 1 and not news[0].real_time
    assert news[0].published_at.hour == 14 and news[0].received_at > news[0].published_at


async def test_alpaca_stream_requires_explicit_entitlement():
    alpaca = AlpacaNewsClient("key", "secret")
    try:
        with pytest.raises(CapabilityUnavailable, match="entitlement"):
            await anext(alpaca.stream(["TEST"]))
    finally:
        await alpaca.aclose()


def test_sec_keeps_missing_acceptance_unknown_and_form_neutral():
    data = {"tickers": ["TEST"], "filings": {"recent": {
        "form": ["S-3", "8-K", "4"], "accessionNumber": ["0000000001-26-000001", "0000000001-26-000002", "0000000001-26-000003"],
        "filingDate": ["2026-11-13"] * 3, "acceptanceDateTime": ["", "2026-11-13T14:00:00Z", ""],
        "primaryDocument": ["registration.htm", "event.htm", "owner.xml"]}}}
    filings = parse_submissions(data, "0000000001", NOW)
    assert len(filings) == 2 and filings[0].published_at is None and filings[0].as_news() is None
    news = filings[1].as_news()
    assert news.title == "SEC 8-K filing" and not news.real_time
    assert news.published_at.hour == 14 and "000000000126000002/event.htm" in news.url


async def test_sec_client_identity_header_and_cik():
    def handle(request):
        assert request.url.path == "/submissions/CIK0000000001.json"
        assert request.headers["User-Agent"] == "PennyQuant developer@example.test"
        return httpx.Response(200, json={"cik": 1, "filings": {"recent": {}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sec = SecEdgarClient("PennyQuant developer@example.test", client=client)
        assert await sec.submissions(1) == []


def test_rss_skips_unknown_dates_and_preserves_explicit_ticker_mapping():
    xml = b'''<rss><channel><item><guid>one</guid><title>TEST FDA approval</title>
        <pubDate>Fri, 13 Nov 2026 14:00:00 GMT</pubDate><link>https://wire.test/one</link></item>
        <item><guid>two</guid><title>No timestamp</title></item></channel></rss>'''
    news = parse_feed(xml, source="wire", received_at=NOW)
    assert len(news) == 1 and news[0].symbols == () and not news[0].verified and not news[0].real_time
    mapped = parse_feed(xml, source="company-feed", received_at=NOW, symbols=("TEST",), verified=True)
    assert mapped[0].symbols == ("TEST",) and mapped[0].verified and mapped[0].published_at.hour == 14
    with pytest.raises(DataQualityError):
        parse_feed(b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss/>', source="wire", received_at=NOW)


def test_atom_revision_time_is_not_fabricated_as_original_publication():
    xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>one</id>
        <title>Old story updated</title><updated>2026-11-13T15:00:00Z</updated></entry></feed>'''
    assert parse_feed(xml, source="wire", received_at=NOW) == []


async def test_toss_ws_rejects_partial_ack_without_silent_reconnect(monkeypatch):
    class FakeWS:
        async def send(self, payload):
            assert isinstance(json.loads(payload), list)
        def __aiter__(self):
            return self
        async def __anext__(self):
            return json.dumps({"type": "subscriptions", "subscribed": ["trade:us:TEST"],
                               "rejected": [{"target": "orderbook:us:TEST", "code": "stock-not-found"}]})
    class Context:
        async def __aenter__(self):
            return FakeWS()
        async def __aexit__(self, *args):
            pass
    def connect(*args, **kwargs):
        assert kwargs["additional_headers"]["Authorization"] == "Bearer fake"
        return Context()
    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    toss = TossMarketClient("id", "secret")
    async def token():
        return "fake"
    toss.access_token = token
    try:
        with pytest.raises(CapabilityUnavailable, match="rejected"):
            await anext(TossWebSocket(toss).stream(["TEST"]))
    finally:
        await toss.aclose()
