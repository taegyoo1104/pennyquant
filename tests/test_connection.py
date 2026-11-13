from __future__ import annotations

import json

import httpx
import pytest

from sessionquant.collectors.toss_market import TossMarketClient
from sessionquant.connection import check_toss, run_check


CLIENT_ID = "private-client-id-should-not-appear"
CLIENT_SECRET = "private-secret-should-not-appear"
TOKEN = "private-token-should-not-appear"


def price(**updates):
    return {"symbol": "AAPL", "lastPrice": "185.70", "currency": "USD",
            "timestamp": "2026-11-13T15:00:00Z", **updates}


def assert_no_secrets(report):
    serialized = json.dumps(report)
    for secret in (CLIENT_ID, CLIENT_SECRET, TOKEN):
        assert secret not in serialized


async def execute(handle, symbol="AAPL"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        adapter = TossMarketClient(CLIENT_ID, CLIENT_SECRET, client=http)
        adapter.limiter.interval = 0
        result = await check_toss(symbol, client=adapter)
        assert not http.is_closed  # injected client remains caller-owned
        assert_no_secrets(result)
        return result


async def test_success_is_authentication_and_price_read_only():
    requests = []
    def handle(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 86400})
        assert request.url.path == "/api/v1/prices"
        assert request.url.params["symbols"] == "AAPL"
        return httpx.Response(200, json={"result": [price(secret=CLIENT_SECRET, token=TOKEN)]})
    result = await execute(handle, " aapl ")
    assert result["success"] is True
    assert result["authentication"] == result["price_read"] == "ok"
    assert result["symbol"] == "AAPL" and result["last_price"] == "185.70"
    assert result["currency"] == "USD"
    assert result["market_timestamp"] == "2026-11-13T15:00:00+00:00"
    assert result["price_received_at"] is not None
    assert not result["realtime_feed_verified"] and not result["paper_trading_ready"]
    assert not result["live_orders_enabled"]
    assert requests == [("POST", "/oauth2/token"), ("GET", "/api/v1/prices")]


@pytest.mark.parametrize("timestamp", [None, "missing"])
async def test_market_time_can_be_unknown_without_inventing_a_live_quote(timestamp):
    item = price(timestamp=timestamp)
    if timestamp == "missing":
        del item["timestamp"]
    def handle(request):
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 86400})
        return httpx.Response(200, json={"result": [item]})
    result = await execute(handle)
    assert result["success"] is True and result["market_timestamp"] is None
    assert not result["realtime_feed_verified"] and not result["paper_trading_ready"]


@pytest.mark.parametrize("stage,status", [("authentication", 401), ("authentication", 403), ("price_read", 401), ("price_read", 403), ("price_read", 404)])
async def test_http_errors_do_not_leak_body_headers_or_request_secrets(stage, status):
    requests = []
    def handle(request):
        requests.append(request.url.path)
        if stage == "price_read" and request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 86400})
        return httpx.Response(status, json={"secret": CLIENT_SECRET, "token": TOKEN},
                              headers={"X-Private": CLIENT_ID})
    result = await execute(handle)
    assert not result["success"]
    assert result["error"]["stage"] == stage and result["error"]["http_status"] == status
    assert result["authentication"] == ("failed" if stage == "authentication" else "ok")
    assert len(requests) == (1 if stage == "authentication" else 2)


@pytest.mark.parametrize("payload", [
    None, {}, [], [price(symbol="OTHER")], [price(), price()],
    [price(lastPrice="NaN")], [price(lastPrice="Infinity")], [price(lastPrice="0")],
    [price(lastPrice=185.70)], [price(lastPrice=CLIENT_SECRET)],
    [price(currency="KRW")], [price(timestamp="2026-11-13T15:00:00")],
    [price(timestamp=123)],
])
async def test_invalid_price_data_never_reports_a_success(payload):
    def handle(request):
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 86400})
        return httpx.Response(200, json={"result": payload})
    result = await execute(handle)
    assert not result["success"] and result["price_read"] == "failed"
    assert result["authentication"] == "ok"
    assert result["error"]["code"] == "invalid_response"
    assert result["last_price"] is None


async def test_network_error_message_is_not_echoed():
    def handle(request):
        raise httpx.ConnectError(CLIENT_SECRET + TOKEN, request=request)
    result = await execute(handle)
    assert not result["success"] and result["error"]["code"] == "network_error"


async def test_unexpected_exception_message_is_not_echoed():
    class FailingAdapter:
        async def access_token(self):
            raise RuntimeError(CLIENT_SECRET + TOKEN)
    result = await check_toss(client=FailingAdapter())
    assert_no_secrets(result)
    assert not result["success"] and result["error"]["code"] == "unexpected_error"


async def test_missing_credentials_are_checked_locally(monkeypatch):
    monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
    monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
    def forbidden_constructor():
        pytest.fail("missing credentials must not open a client or contact provider")
    monkeypatch.setattr("sessionquant.connection.TossMarketClient", forbidden_constructor)
    result = await check_toss()
    assert not result["success"] and result["error"]["code"] == "missing_credentials"
    assert result["missing_environment_variables"] == ["TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"]
    assert result["authentication"] == result["price_read"] == "not_checked"


async def test_owned_client_is_closed_and_cli_returns_nonzero_without_printing_secrets(monkeypatch, capsys):
    monkeypatch.setenv("TOSS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("TOSS_CLIENT_SECRET", CLIENT_SECRET)
    class FailingAdapter:
        closed = False
        async def access_token(self):
            raise ValueError(CLIENT_SECRET + TOKEN)
        async def aclose(self):
            self.closed = True
    adapter = FailingAdapter()
    monkeypatch.setattr("sessionquant.connection.TossMarketClient", lambda: adapter)
    assert await run_check() == 2
    assert adapter.closed
    result = json.loads(capsys.readouterr().out)
    assert not result["success"] and result["authentication"] == "failed"
    assert_no_secrets(result)


@pytest.mark.parametrize("symbol", [None, "", "../secret", "AAPL,MSFT", "X" * 21])
async def test_invalid_symbol_never_contacts_provider(symbol):
    class ForbiddenAdapter:
        async def access_token(self):
            pytest.fail("invalid symbol must not contact provider")
    result = await check_toss(symbol, client=ForbiddenAdapter())
    assert not result["success"] and result["error"]["code"] == "invalid_symbol"
    assert result["symbol"] is None
