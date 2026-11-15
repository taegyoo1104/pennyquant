import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from sessionquant.collectors.alpaca_news import AlpacaNewsClient, parse_news
from sessionquant.collectors.common import CapabilityUnavailable, DataQualityError, RateLimiter, get_readonly
from sessionquant.collectors.rss_collector import parse_feed
from sessionquant.collectors.sec_edgar import SecEdgarClient, parse_submissions
from sessionquant.collectors.toss_market import TossMarketClient, parse_candles, parse_orderbook
from sessionquant.collectors.toss_websocket import TossWebSocket, quote_from_frame, subscription_declaration
from sessionquant.config.settings import Settings
from sessionquant.models import NewsEvent
from sessionquant.news.classifier import classify
from sessionquant.news.event_filter import eligible_events, positive_event

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
    monkeypatch.setattr("sessionquant.collectors.common.asyncio.sleep", sleep)
    def handle(request):
        calls.append(request.method)
        return httpx.Response(429, headers={"Retry-After": "0.7"}) if len(calls) < 3 else httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await get_readonly(client, "https://example.test/news", limiter=RateLimiter(5))
    assert calls == ["GET", "GET", "GET"] and waits.count(0.7) == 2


async def test_alpaca_rest_paginates_deduplicates_but_is_never_marked_live(monkeypatch):
    monkeypatch.setattr("sessionquant.collectors.alpaca_news.now_utc", lambda: NOW)
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


@pytest.mark.parametrize("field,value", [
    ("key", "잘못입력한키"), ("key", "key\n"), ("secret", "secret value"),
    ("secret", "secret\x7f"),
])
async def test_alpaca_bad_credential_format_is_identified_without_request_or_value(field, value):
    def handle(request):
        pytest.fail("Invalid credentials must be rejected before sending an HTTP request")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        alpaca = AlpacaNewsClient(value if field == "key" else "valid-key",
                                  value if field == "secret" else "valid-secret", client=client)
        with pytest.raises(CapabilityUnavailable) as exc:
            await alpaca.fetch(["AAPL"])
    assert ("APCA_API_KEY_ID" if field == "key" else "APCA_API_SECRET_KEY") in str(exc.value)
    assert value not in str(exc.value) and "invalid characters" in str(exc.value)


@pytest.mark.parametrize("updates,field", [
    ({"symbols": None}, "symbols"),
    ({"symbols": "AAPL"}, "symbols"),
    ({"created_at": "PRIVATE-invalid-timestamp"}, "created_at"),
    ({"created_at": "2026-11-13T14:00:00"}, "created_at"),
    ({"headline": None}, "headline"),
    ({"content": {"PRIVATE": "invalid"}}, "content"),
])
def test_alpaca_news_field_diagnostics_do_not_echo_provider_values(updates, field):
    article = {"id": 1, "headline": "Valid headline", "symbols": ["AAPL"],
               "created_at": "2026-11-13T14:00:00Z", **updates}
    with pytest.raises(DataQualityError) as exc:
        parse_news(article, NOW)
    assert f"field: {field}" in str(exc.value)
    assert "SECRET" not in str(exc.value) and "PRIVATE" not in str(exc.value)


def test_alpaca_news_nullable_optional_text_and_fractional_timestamp():
    news = parse_news({"id": 1, "headline": "AAPL news", "symbols": ["AAPL"],
                       "created_at": "2026-11-13T14:00:00.123456789Z",
                       "source": None, "url": None, "content": None, "summary": None}, NOW)
    assert news.source == "" and news.url == "" and news.body == ""
    assert news.published_at.microsecond == 123456 and not news.real_time


def test_alpaca_unsupported_string_tags_preserve_exact_tickers_and_order(caplog):
    news = parse_news({"id": 1, "headline": "Market update", "created_at": NOW.isoformat(),
                       "symbols": ["MSFT", "AAPL", "$PRIVATE", "BRK/B", "MSFT", "aapl", " AAPL",
                                   "BRK.B", "BRK-B", "AAPL"], "source": "verified-wire"}, NOW)
    assert news.symbols == ("MSFT", "AAPL", "BRK.B", "BRK-B")
    assert news.verified and not news.real_time
    assert caplog.records
    assert "$PRIVATE" not in caplog.text and "BRK/B" not in caplog.text


def test_alpaca_individually_malformed_tags_are_omitted_without_coercion(caplog):
    news = parse_news({"id": 1, "headline": "Market update", "created_at": NOW.isoformat(),
                       "symbols": ["AAPL", None, 123, {"PRIVATE": "MSFT"}, ["MSFT"], "MSFT", True]}, NOW)
    assert news.symbols == ("AAPL", "MSFT") and news.verified
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("symbols", [[], ["$AAPL", "aapl", " AAPL", "AAPL "]])
def test_alpaca_unresolved_tags_do_not_fabricate_a_trading_ticker(symbols):
    news = parse_news({"id": 1, "headline": "Company receives FDA approval",
                       "created_at": NOW.isoformat(), "symbols": symbols}, NOW, real_time=True)
    assert news.symbols == ()
    assert eligible_events([news], "AAPL", NOW, Settings()) == []
    assert positive_event([news], "AAPL", NOW, Settings()) is None


def test_alpaca_unsupported_extra_tags_do_not_hide_known_negative_news():
    negative = parse_news({"id": 1, "headline": "Company launches a public offering",
                           "created_at": NOW.isoformat(), "source":"Benzinga", "symbols": ["AAPL", "^PRIVATE"]}, NOW)
    positive = parse_news({"id": 2, "headline": "Company receives FDA approval",
                           "created_at": NOW.isoformat(), "source":"Benzinga", "symbols": ["AAPL"]}, NOW, real_time=True)
    assert negative.symbols == ("AAPL",) and negative.verified
    assert classify(negative).sentiment == "negative"
    assert positive_event([positive, negative], "AAPL", NOW, Settings()) is None


def test_news_domain_model_still_rejects_unsupported_tickers():
    with pytest.raises(ValueError, match="invalid symbol"):
        NewsEvent("n1", ("$AAPL",), "Headline", NOW, NOW, "verified-wire")


async def test_alpaca_rest_collects_article_with_unsupported_42nd_tag_and_later_news(monkeypatch):
    monkeypatch.setattr("sessionquant.collectors.alpaca_news.now_utc", lambda: NOW)
    valid_tags = ["AAPL"] + [f"T{index}" for index in range(40)]
    broad = {"id": 1, "headline": "Broad market update", "created_at": NOW.isoformat(),
             "symbols": valid_tags + ["$PRIVATE", "MSFT"]}
    later = {"id": 2, "headline": "Company launches public offering", "created_at": NOW.isoformat(),
             "symbols": ["AAPL"], "source":"Benzinga"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"news": [broad, later]}),
    )) as client:
        alpaca = AlpacaNewsClient("key", "secret", client=client)
        alpaca.limiter.interval = 0
        news = await alpaca.fetch(["AAPL", "MSFT"])
        assert [item.id for item in news] == ["alpaca:1", "alpaca:2"]
        assert news[0].symbols == tuple(valid_tags + ["MSFT"])
        assert news[0].verified and not news[0].real_time
        assert classify(news[1]).sentiment == "negative"
        assert await alpaca.fetch(["AAPL", "MSFT"]) == []


async def test_alpaca_rest_does_not_assign_query_ticker_missing_from_provider_tags():
    article = {"id": 1, "headline": "Company receives FDA approval", "created_at": NOW.isoformat(),
               "symbols": ["$AAPL", "MSFT"]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"news": [article]}),
    )) as client:
        alpaca = AlpacaNewsClient("key", "secret", client=client)
        news = await alpaca.fetch(["AAPL"])
    assert news[0].symbols == ("MSFT",) and "AAPL" not in news[0].symbols


@pytest.mark.parametrize("body,field", [
    (b"<html>PRIVATE-response</html>", "response"),
    (b'{"news":null}', "news"),
    (b'{"news":[],"next_page_token":[]}', "next_page_token"),
])
async def test_alpaca_bad_response_has_fixed_field_diagnostic(body, field):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))) as client:
        alpaca = AlpacaNewsClient("key", "secret", client=client)
        with pytest.raises(DataQualityError) as exc:
            await alpaca.fetch(["AAPL"])
    assert f"field: {field}" in str(exc.value) and "PRIVATE" not in str(exc.value)


async def test_alpaca_failed_later_page_does_not_consume_unwritten_articles():
    article = {"id": 1, "headline": "AAPL news", "symbols": ["AAPL"],
               "created_at": "2026-11-13T14:00:00Z"}
    invalid_page = True
    def handle(request):
        if request.url.params.get("page_token"):
            return httpx.Response(200, json={"news": [{**article, "id": 2, "created_at": "invalid"}]
                                             if invalid_page else [], "next_page_token": None})
        return httpx.Response(200, json={"news": [article], "next_page_token": "page2"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        alpaca = AlpacaNewsClient("key", "secret", client=client)
        alpaca.limiter.interval = 0
        with pytest.raises(DataQualityError, match="created_at"):
            await alpaca.fetch(["AAPL"])
        assert not alpaca._seen
        invalid_page = False
        assert [news.id for news in await alpaca.fetch(["AAPL"])] == ["alpaca:1"]


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
