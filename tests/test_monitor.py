from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

import httpx
import pytest

from sessionquant.collectors.toss_market import TossMarketClient
from sessionquant.database.repository import KINDS, Repository
from sessionquant.locking import exclusive_lock
from sessionquant.monitor import monitor_toss


CLIENT_ID = "monitor-private-client-id"
CLIENT_SECRET = "monitor-private-client-secret"
TOKEN = "monitor-private-access-token"


def price(symbol="AAPL", **updates):
    return {"symbol": symbol, "lastPrice": "185.700000000000000001", "currency": "USD",
            "timestamp": "2026-11-13T15:00:00Z", **updates}


def snapshot(path):
    repo = Repository(path)
    try:
        return {"tables": {kind: repo.all(kind) for kind in KINDS},
                "controls": {name: json.loads(value) for name, value in repo.db.execute("SELECT name,payload FROM controls")},
                "state": repo.load_state()}
    finally:
        repo.close()


def assert_safe(data):
    serialized = json.dumps(data)
    for secret in (CLIENT_ID, CLIENT_SECRET, TOKEN):
        assert secret not in serialized


@asynccontextmanager
async def adapter_for(handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = TossMarketClient(CLIENT_ID, CLIENT_SECRET, client=http)
        adapter.limiter.interval = 0
        yield adapter, http


async def test_batch_polling_reuses_token_and_saves_only_observed_prices(tmp_path):
    path = tmp_path / "monitor.db"
    requests, ready = [], []
    def on_ready():
        ready.append(snapshot(path)["controls"]["monitor"]["status"])
        assert requests == []
    def handle(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 86400})
        assert request.method == "GET" and request.url.path == "/api/v1/prices"
        assert request.url.params["symbols"] == "AAPL,MSFT"
        if len(requests) == 3:
            assert snapshot(path)["controls"]["monitor"]["status"] == "connected"
        return httpx.Response(200, json={"result": [
            price(secret=CLIENT_SECRET, token=TOKEN, bid="185", volume=123, tradable=True), price("MSFT")],
            "private": CLIENT_ID})
    async with adapter_for(handle) as (adapter, http):
        assert await monitor_toss([" aapl ", "MSFT"], path, max_cycles=2, interval_seconds=1,
                                  client=adapter, on_ready=on_ready) == 0
        assert not http.is_closed
    data = snapshot(path)
    assert ready == ["starting"]
    assert requests == [("POST", "/oauth2/token"), ("GET", "/api/v1/prices"), ("GET", "/api/v1/prices")]
    assert len(data["tables"]["stocks"]) == 2
    assert len(data["tables"]["price_samples"]) == 4
    stock = data["tables"]["stocks"][0]
    assert stock["id"] == stock["symbol"] == "AAPL"
    assert stock["last_price"] == "185.700000000000000001"
    assert stock["timestamp"] == "2026-11-13T15:00:00+00:00"
    assert stock["currency"] == "USD" and stock["source"] == "toss"
    assert stock["received_at"].endswith("+00:00")
    assert set(stock) == {"id", "symbol", "last_price", "currency", "timestamp", "received_at", "source"}
    assert data["controls"]["runtime_kind"] == "toss_monitor"
    status = data["controls"]["monitor"]
    assert status["status"] == "stopped" and status["error"] is None
    assert status["last_success"] == data["controls"]["runtime"]["last_event"] == stock["received_at"]
    assert not status["feed_verified"] and not status["trading_enabled"]
    assert data["controls"]["runtime"] == {"mode": "monitor", "provider": "toss", "strategy": None,
        "last_event": stock["received_at"], "halt_reasons": []}
    assert data["controls"]["source"] == {"synthetic": False,
        "label": "Toss observed prices, partial venues, not NBBO"}
    assert data["state"] is None
    assert all(not rows for kind, rows in data["tables"].items() if kind not in ("stocks", "price_samples"))
    assert_safe(data)


@pytest.mark.parametrize("timestamp", [None, "missing"])
async def test_unknown_provider_time_is_preserved_and_short_token_is_not_preissued_twice(tmp_path, timestamp):
    paths = []
    item = price(timestamp=timestamp)
    if timestamp == "missing":
        del item["timestamp"]
    def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 60})
        return httpx.Response(200, json={"result": [item]})
    async with adapter_for(handle) as (adapter, _):
        assert await monitor_toss(["AAPL"], tmp_path / "prices.db", client=adapter, max_cycles=1) == 0
    data = snapshot(tmp_path / "prices.db")
    assert paths == ["/oauth2/token", "/api/v1/prices"]
    assert data["tables"]["stocks"][0]["timestamp"] is None
    assert data["tables"]["price_samples"][0]["timestamp"] is None


@pytest.mark.parametrize("stage,status", [("authentication", 401), ("authentication", 403),
    ("authentication", 503), ("price_read", 401), ("price_read", 403), ("price_read", 500)])
async def test_http_errors_fail_closed_without_secret_leak_or_token_churn(tmp_path, capsys, stage, status):
    requests = []
    def handle(request):
        requests.append(request.url.path)
        if stage == "price_read" and request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 86400})
        return httpx.Response(status, json={"secret": CLIENT_SECRET, "token": TOKEN},
                              headers={"X-Private": CLIENT_ID})
    async with adapter_for(handle) as (adapter, _):
        assert await monitor_toss(["AAPL"], tmp_path / "prices.db", client=adapter, max_cycles=3) == 2
    data = snapshot(tmp_path / "prices.db")
    assert requests.count("/oauth2/token") == 1
    assert requests.count("/api/v1/prices") == (3 if stage == "price_read" and status == 500 else int(stage == "price_read"))
    monitor = data["controls"]["monitor"]
    assert monitor["status"] == "error" and monitor["last_success"] is None
    assert monitor["error"]["stage"] == stage and monitor["error"]["http_status"] == status
    assert data["tables"]["stocks"] == data["tables"]["price_samples"] == []
    assert_safe(data)
    assert_safe(capsys.readouterr().out)
    assert_safe(capsys.readouterr().err)


async def test_empty_next_poll_retains_price_and_changes_status_independently(tmp_path):
    path = tmp_path / "prices.db"
    polls = 0
    def handle(request):
        nonlocal polls
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 86400})
        polls += 1
        return httpx.Response(200, json={"result": [price()] if polls == 1 else []})
    async with adapter_for(handle) as (adapter, _):
        assert await monitor_toss(["AAPL"], path, client=adapter, interval_seconds=1, max_cycles=4) == 2
    data = snapshot(path)
    assert polls == 2 and len(data["tables"]["price_samples"]) == 1
    stock = data["tables"]["stocks"][0]
    status = data["controls"]["monitor"]
    assert stock["last_price"] == "185.700000000000000001"
    assert status["status"] == "error" and status["error"]["code"] == "no_data"
    assert status["missing_symbols"] == ["AAPL"]
    assert status["last_success"] == data["controls"]["runtime"]["last_event"] == stock["received_at"]


@pytest.mark.parametrize("second,code,missing,invalid", [
    (None, "partial_data", ["MSFT"], []),
    (price("MSFT", lastPrice=CLIENT_SECRET), "invalid_response", [], ["MSFT"]),
])
async def test_partial_results_keep_valid_rows_and_expose_missing_or_invalid_symbols(tmp_path, second, code, missing, invalid):
    def handle(request):
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 86400})
        return httpx.Response(200, json={"result": [price()] + ([second] if second else [])})
    async with adapter_for(handle) as (adapter, _):
        assert await monitor_toss(["AAPL", "MSFT"], tmp_path / "prices.db", client=adapter, max_cycles=3) == 2
    data = snapshot(tmp_path / "prices.db")
    assert [row["symbol"] for row in data["tables"]["stocks"]] == ["AAPL"]
    assert len(data["tables"]["price_samples"]) == 1
    status = data["controls"]["monitor"]
    assert status["status"] == "error" and status["error"]["code"] == code
    assert status["missing_symbols"] == missing and status["invalid_symbols"] == invalid
    assert_safe(data)


@pytest.mark.parametrize("payload,code", [
    ([], "no_data"), ({}, "invalid_response"), (None, "invalid_response"),
    ([price(), price()], "invalid_response"), ([price(lastPrice="0")], "invalid_response"),
    ([price(lastPrice="NaN")], "invalid_response"), ([price(lastPrice="Infinity")], "invalid_response"),
    ([price(lastPrice=185.7)], "invalid_response"), ([price(currency="KRW")], "invalid_response"),
    ([price(timestamp="2026-11-13T15:00:00")], "invalid_response"),
])
async def test_invalid_or_empty_responses_never_claim_connection(tmp_path, payload, code):
    def handle(request):
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 86400})
        return httpx.Response(200, json={"result": payload})
    async with adapter_for(handle) as (adapter, _):
        assert await monitor_toss(["AAPL"], tmp_path / "prices.db", client=adapter, max_cycles=1) == 2
    data = snapshot(tmp_path / "prices.db")
    assert data["controls"]["monitor"]["status"] == "error"
    assert data["controls"]["monitor"]["last_success"] is None
    assert data["controls"]["monitor"]["error"]["code"] == code
    assert data["tables"]["stocks"] == data["tables"]["price_samples"] == []


@pytest.mark.parametrize("existing", ["state", "empty_state", "replay", "follow", "source", "rows"])
async def test_existing_paper_or_unclaimed_database_is_not_relabelled(tmp_path, existing):
    path = tmp_path / "paper.db"
    repo = Repository(path)
    if existing in ("state", "empty_state"):
        repo.save_state({"positions": []} if existing == "state" else {})
    elif existing in ("replay", "follow"):
        repo.set_control("runtime_kind", existing)
    elif existing == "source":
        repo.set_control("source", {"synthetic": True, "label": "demo"})
    else:
        repo.record("stocks", {"id": "DEMO", "symbol": "DEMO", "last_price": 1.0})
    repo.close()
    before = snapshot(path)
    class ForbiddenAdapter:
        async def prices(self, symbols):
            pytest.fail("existing paper database must not contact provider")
    with pytest.raises(ValueError, match="separate Toss monitor database"):
        await monitor_toss(["AAPL"], path, client=ForbiddenAdapter(), max_cycles=1,
                           on_ready=lambda: pytest.fail("rejected DB must not start dashboard"))
    assert snapshot(path) == before


async def test_monitor_uses_same_nonblocking_lease_as_engine(tmp_path):
    path = tmp_path / "locked.db"
    with exclusive_lock(str(path) + ".engine.lock", message="occupied"):
        with pytest.raises(RuntimeError, match="another engine"):
            await monitor_toss(["AAPL"], path, max_cycles=1,
                               on_ready=lambda: pytest.fail("locked DB must not start dashboard"))
    assert not path.exists()


@pytest.mark.parametrize("symbols,interval,max_cycles", [
    ([], 1, 1), (["AAPL"] * 21, 1, 1), (["../secret"], 1, 1), ([None], 1, 1),
    (["AAPL"], 0.9, 1), (["AAPL"], float("nan"), 1), (["AAPL"], float("inf"), 1),
    (["AAPL"], True, 1), (["AAPL"], 1, 0), (["AAPL"], 1, True), (["AAPL"], 1, 1.5),
])
async def test_invalid_configuration_does_not_open_database(tmp_path, symbols, interval, max_cycles):
    path = tmp_path / "invalid.db"
    with pytest.raises(ValueError):
        await monitor_toss(symbols, path, interval_seconds=interval, max_cycles=max_cycles)
    assert not path.exists()


async def test_cancellation_closes_owned_adapter_records_stop_and_releases_lease(tmp_path, monkeypatch):
    path = tmp_path / "cancelled.db"
    observed = asyncio.Event()
    class OwnedAdapter:
        closed = False
        async def prices(self, symbols):
            observed.set()
            return [price()]
        async def aclose(self):
            self.closed = True
    adapter = OwnedAdapter()
    monkeypatch.setenv("TOSS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("TOSS_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr("sessionquant.monitor.TossMarketClient", lambda: adapter)
    task = asyncio.create_task(monitor_toss(["AAPL"], path, interval_seconds=20))
    await asyncio.wait_for(observed.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.closed
    data = snapshot(path)
    assert data["controls"]["monitor"]["status"] == "stopped"
    assert data["controls"]["monitor"]["last_success"] is not None
    with exclusive_lock(str(path) + ".engine.lock", message="must be released"):
        pass


async def test_latest_rows_history_and_success_status_roll_back_together(tmp_path, monkeypatch, capsys):
    path = tmp_path / "atomic.db"
    record = Repository.record
    def fail_second_sample(repo, kind, row):
        if kind == "price_samples" and row["symbol"] == "MSFT":
            raise RuntimeError("disk failure " + CLIENT_SECRET + TOKEN)
        record(repo, kind, row)
    monkeypatch.setattr(Repository, "record", fail_second_sample)
    class Adapter:
        async def prices(self, symbols):
            return [price(), price("MSFT")]
    assert await monitor_toss(["AAPL", "MSFT"], path, client=Adapter(), max_cycles=1) == 2
    data = snapshot(path)
    assert data["tables"]["stocks"] == data["tables"]["price_samples"] == []
    assert data["controls"]["monitor"]["last_success"] is None
    assert data["controls"]["runtime"]["last_event"] is None
    assert data["controls"]["monitor"]["status"] == "error"
    assert data["controls"]["monitor"]["error"]["stage"] == "persistence"
    assert data["controls"]["monitor"]["error"]["code"] == "storage_error"
    assert_safe(data)
    captured = capsys.readouterr()
    assert_safe(captured.out + captured.err)


async def test_missing_local_credentials_produces_safe_error_without_client(tmp_path, monkeypatch):
    monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
    monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("sessionquant.monitor.TossMarketClient", lambda: pytest.fail("no credentials must not open a client"))
    path = tmp_path / "missing.db"
    assert await monitor_toss(["AAPL"], path, max_cycles=1) == 2
    assert snapshot(path)["controls"]["monitor"]["error"]["code"] == "missing_credentials"


async def test_startup_callback_failure_records_stopped_and_releases_lease(tmp_path):
    path = tmp_path / "callback.db"
    def fail():
        raise RuntimeError("dashboard failed")
    with pytest.raises(RuntimeError, match="dashboard failed"):
        await monitor_toss(["AAPL"], path, max_cycles=1, on_ready=fail)
    assert snapshot(path)["controls"]["monitor"]["status"] == "stopped"
    with exclusive_lock(str(path) + ".engine.lock", message="must be released"):
        pass


@pytest.mark.parametrize("failure,code", [("network", "network_error"), ("unexpected", "unexpected_error")])
async def test_exception_details_are_never_stored_or_printed(tmp_path, capsys, failure, code):
    requests = []
    def handle(request):
        requests.append(request.url.path)
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 86400})
        if failure == "network":
            raise httpx.ConnectError(CLIENT_SECRET + TOKEN, request=request)
        raise RuntimeError(CLIENT_ID + CLIENT_SECRET + TOKEN)
    async with adapter_for(handle) as (adapter, _):
        assert await monitor_toss(["AAPL"], tmp_path / "error.db", client=adapter, max_cycles=3) == 2
    data = snapshot(tmp_path / "error.db")
    assert data["controls"]["monitor"]["error"]["code"] == code
    assert data["controls"]["monitor"]["status"] == "error"
    assert requests.count("/oauth2/token") == 1
    assert requests.count("/api/v1/prices") == (3 if failure == "network" else 1)
    assert_safe(data)
    captured = capsys.readouterr()
    assert_safe(captured.out + captured.err)
