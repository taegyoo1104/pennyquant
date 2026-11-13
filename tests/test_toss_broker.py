from datetime import datetime, timezone
from decimal import Decimal
import json

import httpx
import pytest

from pennyquant.collectors.common import CapabilityUnavailable
from pennyquant.collectors.toss_market import TossMarketClient
from pennyquant.database.repository import Repository
from pennyquant.config.settings import Settings
from pennyquant.models import Account, Order, OrderStatus, Quote, Signal
from pennyquant.trading.live_manager import LiveOrderManager
from pennyquant.trading.toss_broker import LimitOrderRequest, TossBroker


def current_quote(symbol="AAPL"):
    now = datetime.now(timezone.utc)
    return Quote(symbol, now, now, 2.0, 2.01, 100, 100)


class BrokerAPI:
    def __init__(self):
        self.requests = []
        self.records = {}
        self.holdings = {}
        self.cash = "1000"
        self.create_error = None
        self.cancel_error = None

    def detail(self, oid, *, status=None, filled=None, price="2.00", commission="0.00", tax="0.00"):
        record = self.records[oid]
        if status is not None:
            record["status"] = status
        if filled is not None:
            record["execution"] = {"filledQuantity": str(filled), "averageFilledPrice": price if filled else None,
                "filledAmount": str(Decimal(price) * filled) if filled else None,
                "commission": commission, "tax": tax,
                "filledAt": datetime.now(timezone.utc).isoformat() if filled else None}
        return record

    def handle(self, request):
        self.requests.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        path = request.url.path
        if path == "/api/v1/accounts":
            return httpx.Response(200, json={"result": [{"accountSeq": 1, "accountType": "BROKERAGE", "accountNo": "PRIVATE"}]})
        assert request.headers["X-Tossinvest-Account"] == "1"
        if path == "/api/v1/holdings":
            return httpx.Response(200, json={"result": {"items": [
                {"symbol": s, "quantity": str(q), "marketCountry": "US", "currency": "USD"}
                for s, q in self.holdings.items()]}})
        if path == "/api/v1/buying-power":
            assert request.url.params["currency"] == "USD"
            return httpx.Response(200, json={"result": {"currency": "USD", "cashBuyingPower": self.cash}})
        if path == "/api/v1/sellable-quantity":
            return httpx.Response(200, json={"result": {"sellableQuantity": str(self.holdings.get(request.url.params["symbol"], 0))}})
        if path == "/api/v1/commissions":
            return httpx.Response(200, json={"result": [{"marketCountry": "US", "commissionRate": "0.001", "startDate": None, "endDate": None}]})
        if path == "/api/v1/orders" and request.method == "GET":
            opened = [r for r in self.records.values() if r["status"] in {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL"}]
            return httpx.Response(200, json={"result": {"orders": opened, "hasNext": False, "nextCursor": None}})
        if path == "/api/v1/orders" and request.method == "POST":
            if self.create_error == "timeout":
                raise httpx.ReadTimeout("SECRET provider token", request=request)
            if isinstance(self.create_error, int):
                return httpx.Response(self.create_error, json={"error": {"message": "SECRET"}})
            payload = json.loads(request.content)
            oid = f"order-{len(self.records) + 1}"
            self.records[oid] = {"orderId": oid, "symbol": payload["symbol"], "side": payload["side"],
                "quantity": payload["quantity"], "price": payload["price"], "currency": "USD",
                "orderType": "LIMIT", "timeInForce": "DAY", "status": "PENDING",
                "orderedAt": datetime.now(timezone.utc).isoformat(),
                "execution": {"filledQuantity": "0", "averageFilledPrice": None,
                              "filledAmount": None, "commission": None, "tax": None, "filledAt": None}}
            return httpx.Response(200, json={"result": {"orderId": oid, "clientOrderId": payload["clientOrderId"]}})
        if path.endswith("/cancel"):
            if self.cancel_error:
                raise httpx.ReadTimeout("SECRET cancel token", request=request)
            oid = path.split("/")[-2]
            self.records[oid]["status"] = "PENDING_CANCEL"
            return httpx.Response(200, json={"result": {"orderId": f"cancel-{oid}"}})
        if path.startswith("/api/v1/orders/"):
            return httpx.Response(200, json={"result": self.records[path.split("/")[-1]]})
        pytest.fail(f"Unexpected broker path {path}")


@pytest.fixture
def api():
    return BrokerAPI()


@pytest.fixture
async def broker(tmp_path, api):
    repo = Repository(tmp_path / "broker.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.handle)) as client:
        market = TossMarketClient("unused", "unused", 1, client=client)
        market._token, market._token_deadline = "SECRET-access", float("inf")
        market.limiter.interval = 0
        result = TossBroker(market, repo, live_enabled=True, acknowledge_live_orders=True)
        yield result
    repo.close()


@pytest.mark.parametrize("updates", [
    {"client_order_id": "invalid/value"}, {"quantity": 0}, {"quantity": 1.5},
    {"quantity": True}, {"limit_price": "2.001"}, {"limit_price": "NaN"}, {"side": "SHORT"},
])
def test_order_request_rejects_invalid_units_and_tick(updates):
    with pytest.raises(ValueError):
        LimitOrderRequest(**{"client_order_id": "pq-1", "symbol": "AAPL", "side": "BUY",
                             "quantity": 1, "limit_price": "2.01", **updates})


async def test_preview_and_unarmed_adapter_never_posts(broker, api):
    request = LimitOrderRequest("pq-1", "AAPL", "BUY", 1, "2.01")
    preview = broker.preview_limit(request)
    assert preview["mode"] == "preview" and not api.requests
    broker.acknowledge_live_orders = False
    with pytest.raises(CapabilityUnavailable, match="acknowledge"):
        await broker.submit_limit(request)
    assert not api.requests


async def test_create_exact_schema_and_durable_client_id_dedup(broker, api):
    assert await broker.reconcile()
    req = LimitOrderRequest("pq-1", "AAPL", "BUY", 2, "2.01")
    item = await broker.submit_limit(req, quote=current_quote(), risk_approved=True, eligibility_verified=True)
    assert item["order_id"] == "order-1" and item["filled_quantity"] == 0
    assert api.requests[-1] == ("POST", "/api/v1/orders", req.payload())
    again = await broker.submit_limit(req)
    assert again == item
    assert len([r for r in api.requests if r[0] == "POST"]) == 1
    restored = TossBroker(broker.market, broker.repository, live_enabled=True, acknowledge_live_orders=True)
    assert await restored.submit_limit(req) == item
    assert len([r for r in api.requests if r[0] == "POST"]) == 1
    assert "PRIVATE" not in json.dumps(broker.repository.get_control("broker_state"))


async def test_timeout_unknown_is_durable_and_never_retried(broker, api):
    assert await broker.reconcile()
    api.create_error = "timeout"
    req = LimitOrderRequest("pq-timeout", "AAPL", "BUY", 1, "2.01")
    with pytest.raises(CapabilityUnavailable, match="unknown") as exc:
        await broker.submit_limit(req, quote=current_quote(), risk_approved=True, eligibility_verified=True)
    assert "SECRET" not in str(exc.value)
    restored = TossBroker(broker.market, broker.repository, live_enabled=True, acknowledge_live_orders=True)
    assert not await restored.reconcile()
    assert (await restored.submit_limit(req))["status"] == "UNKNOWN"
    assert len([r for r in api.requests if r[0] == "POST"]) == 1


async def test_crashed_submitting_intent_cannot_be_rearmed(broker):
    broker.orders["pq-crash"] = {"request": LimitOrderRequest("pq-crash", "AAPL", "BUY", 1, "2.01").payload(),
                                 "status": "SUBMITTING", "order_id": None}
    broker._save()
    restored = TossBroker(broker.market, broker.repository, live_enabled=True, acknowledge_live_orders=True)
    assert restored.orders["pq-crash"]["status"] == "UNKNOWN"
    assert not await restored.reconcile()


@pytest.mark.parametrize("http_status,expected", [(422, "REJECTED"), (409, "UNKNOWN"), (503, "UNKNOWN")])
async def test_create_errors_never_retry_and_ambiguous_states_halt(broker, api, http_status, expected):
    assert await broker.reconcile()
    api.create_error = http_status
    try:
        await broker.submit_limit(LimitOrderRequest("pq-err", "AAPL", "BUY", 1, "2.01"),
                                  quote=current_quote(), risk_approved=True, eligibility_verified=True)
    except CapabilityUnavailable:
        pass
    assert broker.orders["pq-err"]["status"] == expected
    assert len([r for r in api.requests if r[0] == "POST"]) == 1


async def test_unmanaged_holdings_and_orders_block_new_submissions(broker, api):
    api.holdings["MSFT"] = 1
    assert not await broker.reconcile()
    with pytest.raises(CapabilityUnavailable, match="Reconcile"):
        await broker.submit_limit(LimitOrderRequest("pq-1", "AAPL", "BUY", 1, "2.01"),
                                  quote=current_quote(), risk_approved=True, eligibility_verified=True)
    assert not [r for r in api.requests if r[0] == "POST"]


async def test_partial_fill_and_cancel_acknowledgment_do_not_fake_completed_cancel(broker, api):
    assert await broker.reconcile()
    await broker.submit_limit(LimitOrderRequest("pq-1", "AAPL", "BUY", 5, "2.01"),
                              quote=current_quote(), risk_approved=True, eligibility_verified=True)
    api.detail("order-1", status="PARTIAL_FILLED", filled=2)
    api.holdings["AAPL"] = 2
    assert await broker.reconcile()
    item = await broker.cancel_order("pq-1")
    assert item["status"] == "PENDING_CANCEL" and item["cancel_operation_id"] == "cancel-order-1"
    assert item["filled_quantity"] == 2
    api.detail("order-1", status="CANCELED", filled=3)
    api.holdings["AAPL"] = 3
    assert await broker.reconcile()
    assert broker.orders["pq-1"]["filled_quantity"] == 3 and broker.orders["pq-1"]["status"] == "CANCELED"


async def test_sell_cannot_exceed_actual_partial_filled_ownership(broker, api):
    assert await broker.reconcile()
    await broker.submit_limit(LimitOrderRequest("pq-buy", "AAPL", "BUY", 5, "2.01"),
                              quote=current_quote(), risk_approved=True, eligibility_verified=True)
    api.detail("order-1", status="CANCELED", filled=2)
    api.holdings["AAPL"] = 2
    assert await broker.reconcile()
    with pytest.raises(CapabilityUnavailable, match="exceeds"):
        await broker.submit_limit(LimitOrderRequest("pq-sell", "AAPL", "SELL", 3, "2.00"),
                                  quote=current_quote(), risk_approved=True, eligibility_verified=True)


async def test_actual_commission_rate_and_buying_power_readonly(broker):
    assert (await broker.commissions())[0]["commissionRate"] == "0.001"
    assert await broker.buying_power() == Decimal("1000")


async def test_sixth_position_cannot_be_submitted(broker, api):
    assert await broker.reconcile()
    for index in range(5):
        symbol = f"T{index}"
        await broker.submit_limit(LimitOrderRequest(f"pq-{index}", symbol, "BUY", 1, "2.01"),
                                  quote=current_quote(symbol), risk_approved=True, eligibility_verified=True)
        api.detail(f"order-{index + 1}", status="FILLED", filled=1)
        api.holdings[symbol] = 1
        assert await broker.reconcile()
    with pytest.raises(CapabilityUnavailable, match="Position limit"):
        await broker.submit_limit(LimitOrderRequest("pq-sixth", "T5", "BUY", 1, "2.01"),
                                  quote=current_quote("T5"), risk_approved=True, eligibility_verified=True)
    assert len([r for r in api.requests if r[0] == "POST"]) == 5


async def test_unverified_eligibility_or_quote_cannot_send_an_order(broker, api):
    assert await broker.reconcile()
    req = LimitOrderRequest("pq-1", "AAPL", "BUY", 1, "2.01")
    with pytest.raises(CapabilityUnavailable, match="verified eligibility"):
        await broker.submit_limit(req, quote=current_quote(), risk_approved=True)
    quote = current_quote()
    quote = Quote(quote.symbol, quote.timestamp, quote.received_at, 2, 2.01, 100, 100, tradable=False)
    with pytest.raises(CapabilityUnavailable, match="fresh verified"):
        await broker.submit_limit(req, quote=quote, risk_approved=True, eligibility_verified=True)
    assert not [r for r in api.requests if r[0] == "POST"]


async def test_cancel_timeout_stays_blocked_until_original_order_is_terminal(broker, api):
    assert await broker.reconcile()
    await broker.submit_limit(LimitOrderRequest("pq-1", "AAPL", "BUY", 1, "2.01"),
                              quote=current_quote(), risk_approved=True, eligibility_verified=True)
    api.cancel_error = True
    with pytest.raises(CapabilityUnavailable, match="unknown"):
        await broker.cancel_order("pq-1")
    assert not await broker.reconcile()
    assert len([r for r in api.requests if r[1].endswith("/cancel")]) == 1
    api.detail("order-1", status="CANCELED", filled=0)
    assert await broker.reconcile()


async def test_inconsistent_fill_data_halts(broker, api):
    assert await broker.reconcile()
    await broker.submit_limit(LimitOrderRequest("pq-1", "AAPL", "BUY", 2, "2.01"),
                              quote=current_quote(), risk_approved=True, eligibility_verified=True)
    api.detail("order-1", status="FILLED", filled=2)
    api.records["order-1"]["execution"]["filledAmount"] = "9000"
    api.holdings["AAPL"] = 2
    assert not await broker.reconcile() and "unknown_order" in broker.halt_reasons


async def prepared_manager(broker, *, fee_model="legacy"):
    settings = Settings(initial_cash=70.0, regular_hours_only=False,
                        fee_model=fee_model, exit_price_policy="net" if fee_model == "toss_us" else "gross")
    manager = LiveOrderManager(settings, Account(), broker)
    assert await manager.synchronize()
    return manager


async def test_live_quotes_never_simulate_fills_and_only_actual_partial_fills_are_booked(broker, api):
    manager = await prepared_manager(broker)
    quote = current_quote()
    order = Order("pq-live", "AAPL", "buy", 5, 2.01, quote.timestamp, OrderStatus.OPEN)
    manager.orders[order.client_id] = order
    assert manager.on_quote(quote, quote.received_at) == []
    assert not manager.fills and not manager.account.positions
    await manager.drain()
    assert not manager.fills and manager.account.cash == 70
    api.detail("order-1", status="PARTIAL_FILLED", filled=2)
    api.holdings["AAPL"] = 2
    assert await manager.synchronize()
    assert manager.account.positions["AAPL"].qty == 2
    assert manager.orders["pq-live"].filled_qty == 2
    assert manager.account.cash == 66
    assert len(manager.fills) == 1
    assert await manager.synchronize()  # Same cumulative execution cannot book twice.
    assert len(manager.fills) == 1 and manager.account.cash == 66


async def test_live_halt_queues_actual_cancel_and_keeps_order_active_until_confirmation(broker, api):
    manager = await prepared_manager(broker)
    quote = current_quote()
    manager.on_quote(quote, quote.received_at)
    manager.orders["pq-live"] = Order("pq-live", "AAPL", "buy", 2, 2.01, quote.timestamp, OrderStatus.OPEN)
    await manager.drain()
    manager.halt("manual")
    assert manager.orders["pq-live"].status == OrderStatus.OPEN
    assert "pq-live" in manager.pending_cancels
    await manager.drain()
    assert manager.orders["pq-live"].status != OrderStatus.CANCELLED
    assert broker.orders["pq-live"]["status"] == "PENDING_CANCEL"
    api.detail("order-1", status="CANCELED", filled=0)
    assert await manager.synchronize()
    assert manager.orders["pq-live"].status == OrderStatus.CANCELLED


async def test_live_pending_execution_fees_are_conservative_and_then_reconciled(broker, api):
    manager = await prepared_manager(broker, fee_model="toss_us")
    quote = current_quote()
    manager.on_quote(quote, quote.received_at)
    manager.orders["pq-live"] = Order("pq-live", "AAPL", "buy", 6, 2.01, quote.timestamp, OrderStatus.OPEN)
    await manager.drain()
    api.detail("order-1", status="FILLED", filled=6, commission=None, tax=None)
    api.holdings["AAPL"] = 6
    assert await manager.synchronize()
    assert manager.account.positions["AAPL"].qty == 6
    assert "broker_execution_fee_pending" in manager.halt_reasons
    assert manager.account.cash == pytest.approx(57.98)
    api.detail("order-1", status="FILLED", filled=6, commission="0.01", tax="0")
    assert await manager.synchronize()
    assert "broker_execution_fee_pending" not in manager.halt_reasons
    assert manager.account.cash == pytest.approx(57.99)
    position = manager.account.positions["AAPL"]
    assert position.entry_fee == pytest.approx(0.01)
    assert (position.stop_price, position.target_price) == manager.costs.exit_levels(2.0, 6, position.entry_fee)


async def test_live_state_cannot_restore_paper_state_and_preserves_actual_fill_dedup(broker, api):
    manager = await prepared_manager(broker)
    quote = current_quote()
    manager.on_quote(quote, quote.received_at)
    manager.orders["pq-live"] = Order("pq-live", "AAPL", "buy", 2, 2.01, quote.timestamp, OrderStatus.OPEN)
    await manager.drain()
    api.detail("order-1", status="FILLED", filled=2)
    api.holdings["AAPL"] = 2
    assert await manager.synchronize()
    state = manager.export_state()
    assert state["execution_mode"] == "live"
    restored = LiveOrderManager(manager.settings, Account(), broker)
    with pytest.raises(ValueError, match="paper"):
        restored.restore_state({**state, "execution_mode": "paper"})
    restored.restore_state(state)
    assert await restored.synchronize()
    assert len(restored.fills) == 1 and restored.account.positions["AAPL"].qty == 2


async def test_live_allocation_caps_strategy_budget_to_verified_buying_power(broker, api):
    api.cash = "25.00"
    manager = await prepared_manager(broker)
    assert manager.account.cash == manager.account.equity == manager.account.day_start_equity == 25
    assert manager.execution_mode == "live"


async def test_live_weekend_cannot_reset_daily_stop(broker):
    manager = await prepared_manager(broker)
    manager.roll_day(datetime(2026, 11, 13, 15, tzinfo=timezone.utc))
    manager.halt("daily_loss")
    manager.roll_day(datetime(2026, 11, 14, 15, tzinfo=timezone.utc))
    assert "daily_loss" in manager.halt_reasons
    assert manager._day == "2026-11-13"


async def test_real_strategy_signal_id_and_price_reach_broker_with_valid_units(broker, api):
    manager = await prepared_manager(broker)
    now = datetime.now(timezone.utc)
    quote = Quote("AAPL", now, now, 2.08, 2.082, 1000, 1000)
    manager.on_quote(quote, now)
    signal = Signal(id="f" * 64, symbol="AAPL", timestamp=now, strategy="momentum",
                    reason="confirmed primary source and momentum", reference_price=2.082)
    order = manager.submit(signal, quote, now)
    assert order is not None and len(order.client_id) <= 36
    assert Decimal(str(order.limit_price)) == Decimal("2.09")
    before = len([request for request in api.requests if request[1] == "/api/v1/accounts"])
    await manager.drain()
    assert len([request for request in api.requests if request[1] == "/api/v1/accounts"]) == before
    post = [request for request in api.requests if request[0] == "POST"]
    assert len(post) == 1 and post[0][2]["clientOrderId"] == order.client_id
    assert manager.account.positions == {} and manager.fills == []
    api.detail("order-1", status="FILLED", filled=order.qty, price="2.08")
    api.holdings["AAPL"] = order.qty
    assert await manager.synchronize()
    assert manager.account.positions["AAPL"].qty == order.qty
    assert manager.account.positions["AAPL"].avg_price == pytest.approx(2.08)
