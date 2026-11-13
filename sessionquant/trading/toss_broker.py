"""Explicitly armed Toss US whole-share LIMIT/DAY orders.

Contract: official REST OpenAPI 1.2.28, checked 2026-11-13. The server's
clientOrderId cache expires after TEN MINUTES. This durable local journal never
reuses an identifier, and never retries an ambiguous POST. Order history does
not expose clientOrderId, so a create timeout without orderId needs operator
reconciliation. The read-only market adapter remains read-only.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any

import httpx

from ..collectors.common import CapabilityUnavailable, DataQualityError
from ..collectors.toss_market import BASE_URL, TossMarketClient
from ..database.repository import Repository
from ..models import Quote, symbol_valid, utc

TERMINAL = {"FILLED", "CANCELED", "REJECTED"}
ACTIVE = {"SUBMITTING", "PENDING", "PARTIAL_FILLED", "PENDING_CANCEL"}
KNOWN = TERMINAL | ACTIVE


def decimal_value(value, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DataQualityError("Broker returned an invalid numeric value") from None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise DataQualityError("Broker returned an invalid numeric value")
    return result


def whole_quantity(value) -> int:
    result = decimal_value(value)
    if result != result.to_integral_value():
        raise DataQualityError("Fractional holdings/orders require separate reconciliation")
    return int(result)


@dataclass(frozen=True)
class LimitOrderRequest:
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: Decimal | str | float

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", self.client_order_id):
            raise ValueError("client_order_id must be 1-36 alphanumeric, dash or underscore characters")
        symbol_valid(self.symbol)
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("live orders require a positive whole-share quantity")
        price = decimal_value(self.limit_price, positive=True)
        tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
        if price != price.quantize(tick):
            raise ValueError("US limit price must match the documented price tick")
        object.__setattr__(self, "limit_price", price)

    def payload(self) -> dict:
        return {"clientOrderId": self.client_order_id, "symbol": self.symbol,
                "side": self.side, "orderType": "LIMIT", "timeInForce": "DAY",
                "quantity": str(self.quantity), "price": format(self.limit_price, "f")}


class TossBroker:
    def __init__(self, market: TossMarketClient, repository: Repository | None = None, *,
                 live_enabled: bool = False, acknowledge_live_orders: bool = False,
                 max_positions: int = 5, max_quote_age_seconds: float = 5,
                 reconciliation_ttl_seconds: float = 30):
        if not 1 <= max_positions <= 5:
            raise ValueError("live max_positions must be between one and five")
        if live_enabled and repository is None:
            raise ValueError("Live orders require a durable repository")
        self.market, self.repository = market, repository
        self.live_enabled, self.acknowledge_live_orders = live_enabled, acknowledge_live_orders
        self.max_positions = max_positions
        self.max_quote_age_seconds = max_quote_age_seconds
        self.reconciliation_ttl_seconds = reconciliation_ttl_seconds
        self._lock = asyncio.Lock()
        saved = repository.get_control("toss_live_broker", {}) if repository else {}
        if saved and saved.get("account_seq") != market.account_seq:
            raise ValueError("Broker journal belongs to a different account")
        self.orders: dict[str, dict] = dict(saved.get("orders", {}))
        self.halt_reasons: set[str] = set(saved.get("halt_reasons", []))
        for item in self.orders.values():
            if item.get("status") == "SUBMITTING":
                item["status"] = "UNKNOWN"
                self.halt_reasons.add("unknown_order")
        self.reconciled = False
        self.reconciled_at: datetime | None = None
        self.positions: dict[str, int] = {}
        self.cash_buying_power: Decimal | None = None

    @staticmethod
    def preview_limit(request: LimitOrderRequest) -> dict:
        return {"mode": "preview", "live_orders_enabled": False, "request": request.payload()}

    def _require_live(self):
        if not self.live_enabled or not self.acknowledge_live_orders:
            raise CapabilityUnavailable("Actual orders require --live and --acknowledge-live-orders")
        if self.market.account_seq is None:
            raise CapabilityUnavailable("Set TOSS_ACCOUNT_SEQ before actual orders")

    def _save(self):
        if not self.repository:
            return
        self.repository.set_control("toss_live_broker", {"version": 1,
            "account_seq": self.market.account_seq, "orders": self.orders,
            "halt_reasons": sorted(self.halt_reasons)})
        self.repository.set_control("broker_state", {
            "mode": "live" if self.live_enabled else "preview", "reconciled": self.reconciled,
            "status": "ready" if self.reconciled and not self.halt_reasons else "blocked",
            "halt_reasons": sorted(self.halt_reasons), "account_seq": self.market.account_seq,
            "positions": self.positions,
            "cash_buying_power": str(self.cash_buying_power) if self.cash_buying_power is not None else None,
            "observed_at": self.reconciled_at.isoformat() if self.reconciled_at else None,
            "orders": list(self.orders.values()),
        })

    def owned_quantities(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in self.orders.values():
            qty = int(item.get("filled_quantity", 0))
            sign = 1 if item["request"]["side"] == "BUY" else -1
            symbol = item["request"]["symbol"]
            result[symbol] = result.get(symbol, 0) + sign * qty
        if any(qty < 0 for qty in result.values()):
            raise DataQualityError("Broker journal contains negative managed holdings")
        return {symbol: qty for symbol, qty in result.items() if qty}

    async def commissions(self) -> list[dict]:
        result = await self.market._get("/api/v1/commissions", account=True)
        if not isinstance(result, list):
            raise DataQualityError("Broker commission response must be an array")
        return [{"marketCountry": item["marketCountry"],
                 "commissionRate": str(decimal_value(item["commissionRate"])),
                 "startDate": item.get("startDate"), "endDate": item.get("endDate")}
                for item in result if isinstance(item, dict) and item.get("marketCountry") == "US"]

    async def buying_power(self) -> Decimal:
        value = await self.market._get("/api/v1/buying-power", params={"currency": "USD"}, account=True)
        if not isinstance(value, dict) or value.get("currency") != "USD":
            raise DataQualityError("Broker buying power requires USD")
        self.cash_buying_power = decimal_value(value.get("cashBuyingPower"))
        return self.cash_buying_power

    async def sellable_quantity(self, symbol: str) -> Decimal:
        symbol_valid(symbol)
        value = await self.market._get("/api/v1/sellable-quantity", params={"symbol": symbol}, account=True)
        return decimal_value(value.get("sellableQuantity"))

    def _apply_detail(self, item: dict, detail: dict):
        request = item["request"]
        if (not isinstance(detail, dict) or detail.get("orderId") != item.get("order_id")
                or detail.get("symbol") != request["symbol"] or detail.get("side") != request["side"]
                or detail.get("currency") != "USD" or detail.get("orderType") != "LIMIT"
                or detail.get("timeInForce") != "DAY"
                or whole_quantity(detail.get("quantity")) != int(request["quantity"])
                or decimal_value(detail.get("price"), positive=True) != decimal_value(request["price"])):
            raise DataQualityError("Broker order detail differs from the submitted intent")
        execution = detail.get("execution")
        if not isinstance(execution, dict):
            raise DataQualityError("Broker order detail lacks execution information")
        filled = whole_quantity(execution.get("filledQuantity"))
        if filled < item.get("filled_quantity", 0) or filled > int(request["quantity"]):
            raise DataQualityError("Broker cumulative fill quantity is inconsistent")
        status = detail.get("status")
        if status not in KNOWN or status == "SUBMITTING":
            raise DataQualityError("Broker order state requires separate reconciliation")
        if status == "FILLED" and filled != int(request["quantity"]):
            raise DataQualityError("Broker full-fill status has an inconsistent quantity")
        avg = execution.get("averageFilledPrice")
        amount = execution.get("filledAmount")
        if filled:
            average = decimal_value(avg, positive=True)
            notional = decimal_value(amount, positive=True)
            if abs(average * filled - notional) > max(Decimal("0.01"), Decimal(filled) * Decimal("0.0001")):
                raise DataQualityError("Broker average fill price and notional are inconsistent")
            if execution.get("filledAt") is not None:
                try:
                    utc(execution["filledAt"])
                except (ValueError, TypeError, AttributeError, OverflowError):
                    raise DataQualityError("Broker fill timestamp is invalid") from None
        item.update({"status": status, "filled_quantity": filled,
            "average_filled_price": str(decimal_value(avg)) if avg is not None else None,
            "filled_amount": str(decimal_value(amount)) if amount is not None else None,
            "commission": str(decimal_value(execution["commission"])) if execution.get("commission") is not None else None,
            "tax": str(decimal_value(execution["tax"])) if execution.get("tax") is not None else None,
            "filled_at": execution.get("filledAt"), "cancel_operation_id": item.get("cancel_operation_id")})

    async def refresh_order(self, client_order_id: str) -> dict:
        item = self.orders[client_order_id]
        if not item.get("order_id"):
            raise CapabilityUnavailable("Unknown create outcome requires operator reconciliation; do not resubmit")
        try:
            detail = await self.market._get(f"/api/v1/orders/{item['order_id']}", account=True)
            self._apply_detail(item, detail)
        except (ValueError, KeyError, TypeError, httpx.HTTPError):
            self.halt_reasons.add("unknown_order")
            self.reconciled = False
            self._save()
            raise CapabilityUnavailable("Broker order refresh failed; automatic submissions are paused") from None
        self._save()
        return dict(item)

    async def reconcile(self) -> bool:
        """Reconcile owned US holdings and all open orders before any live write."""
        async with self._lock:
            return await self._reconcile()

    async def _reconcile(self) -> bool:
        self.reconciled = False
        try:
            if self.market.account_seq is None:
                raise DataQualityError("Account selection is missing")
            accounts = await self.market.accounts()
            if not any(isinstance(a, dict) and a.get("accountSeq") == self.market.account_seq
                       and a.get("accountType") == "BROKERAGE" for a in accounts):
                raise DataQualityError("Selected account is not a verified brokerage account")
            unknown_without_id = [item for item in self.orders.values()
                                  if item["status"] == "UNKNOWN" and not item.get("order_id")]
            if unknown_without_id:
                self.halt_reasons.add("unknown_order")
                self._save()
                return False
            for cid, item in self.orders.items():
                if item.get("order_id") and (item["status"] not in TERMINAL
                        or (item.get("filled_quantity", 0) and (item.get("commission") is None or item.get("tax") is None))):
                    await self.refresh_order(cid)
            if any(item.get("cancel_requested") and not item.get("cancel_operation_id")
                   and item["status"] not in TERMINAL for item in self.orders.values()):
                self.halt_reasons.add("unknown_order")
                self._save()
                return False
            holdings = await self.market.holdings()
            if not isinstance(holdings, dict) or not isinstance(holdings.get("items"), list):
                raise DataQualityError("Broker holdings response is incomplete")
            positions = {}
            for item in holdings["items"]:
                if item.get("marketCountry") != "US" or item.get("currency") != "USD":
                    raise DataQualityError("Mixed-market account requires explicit external-position handling")
                symbol_valid(item["symbol"])
                qty = whole_quantity(item["quantity"])
                if qty:
                    if item["symbol"] in positions:
                        raise DataQualityError("Duplicate broker holding")
                    positions[item["symbol"]] = qty
            opened = await self.market.orders("OPEN")
            if (not isinstance(opened, dict) or not isinstance(opened.get("orders"), list)
                    or opened.get("hasNext") is not False):
                raise DataQualityError("Broker open-order snapshot is incomplete")
            actual_open = {item["orderId"] for item in opened["orders"]}
            expected_open = {item["order_id"] for item in self.orders.values()
                             if item.get("order_id") and item["status"] in ACTIVE}
            if positions != self.owned_quantities() or actual_open != expected_open:
                self.halt_reasons.add("reconciliation_mismatch")
                self.positions = positions
                self._save()
                return False
            self.positions = positions
            await self.buying_power()
            self.halt_reasons.discard("reconciliation_mismatch")
            self.halt_reasons.discard("unknown_order")
            self.halt_reasons.discard("broker_read_error")
            self.reconciled = not self.halt_reasons
            self.reconciled_at = datetime.now(timezone.utc)
            self._save()
            return self.reconciled
        except (ValueError, KeyError, TypeError, httpx.HTTPError, CapabilityUnavailable):
            self.halt_reasons.add("broker_read_error")
            self._save()
            return False

    def _valid_quote(self, request: LimitOrderRequest, quote: Quote | None):
        now = datetime.now(timezone.utc)
        if (quote is None or quote.symbol != request.symbol or not quote.tradable or quote.halted
                or quote.exchange.upper() not in {"NASDAQ", "NYSE", "AMEX", "NYSEAMERICAN", "NYSE ARCA", "ARCA", "NYSEARCA"}
                or utc(quote.timestamp) > utc(quote.received_at) or utc(quote.received_at) > now
                or (now - utc(quote.timestamp)).total_seconds() > self.max_quote_age_seconds
                or (now - utc(quote.received_at)).total_seconds() > self.max_quote_age_seconds):
            raise CapabilityUnavailable("Actual orders require a fresh verified tradable quote")

    async def submit_limit(self, request: LimitOrderRequest, *, quote: Quote | None = None,
                           risk_approved: bool = False, eligibility_verified: bool = False) -> dict:
        self._require_live()
        async with self._lock:
            payload = request.payload()
            existing = self.orders.get(request.client_order_id)
            if existing:
                if existing["request"] != payload:
                    self.halt_reasons.add("idempotency_conflict")
                    self.reconciled = False
                    self._save()
                    raise CapabilityUnavailable("Client order identifier was reused for a different intent")
                # Never issue a second POST, including after the server's ten-minute cache.
                return dict(existing)
            now = datetime.now(timezone.utc)
            if (not self.reconciled or self.halt_reasons or self.reconciled_at is None
                    or (now - self.reconciled_at).total_seconds() > self.reconciliation_ttl_seconds):
                raise CapabilityUnavailable("Reconcile broker holdings and orders before submitting")
            if risk_approved is not True or eligibility_verified is not True:
                raise CapabilityUnavailable("Actual orders require verified eligibility and an approved risk decision")
            occupied = set(self.positions) | {i["request"]["symbol"] for i in self.orders.values()
                        if i["request"]["side"] == "BUY" and i["status"] in ACTIVE}
            if request.side == "BUY":
                if request.symbol in occupied or len(occupied) >= self.max_positions:
                    raise CapabilityUnavailable("Position limit or existing position blocks this entry")
                if request.limit_price * request.quantity > await self.buying_power():
                    raise CapabilityUnavailable("Insufficient cash buying power")
            else:
                reserved = sum(int(i["request"]["quantity"]) - i.get("filled_quantity", 0)
                               for i in self.orders.values() if i["request"]["symbol"] == request.symbol
                               and i["request"]["side"] == "SELL" and i["status"] in ACTIVE)
                owned = self.positions.get(request.symbol, 0) - reserved
                sellable = await self.sellable_quantity(request.symbol)
                if request.quantity > owned or request.quantity > sellable:
                    raise CapabilityUnavailable("Sell quantity exceeds reconciled owned/sellable shares")
            token = await self.market.access_token()
            await self.market.limiter.wait()
            self._valid_quote(request, quote)
            item = {"client_order_id": request.client_order_id, "request": payload, "status": "SUBMITTING",
                    "order_id": None, "filled_quantity": 0, "average_filled_price": None,
                    "filled_amount": None, "commission": None, "tax": None,
                    "submitted_at": now.isoformat()}
            self.orders[request.client_order_id] = item
            self._save()  # Write intent durably BEFORE the sole network mutation.
            try:
                response = await self.market.client.post(BASE_URL + "/api/v1/orders", json=payload,
                    headers={"Authorization": "Bearer " + token,
                             "X-Tossinvest-Account": str(self.market.account_seq)})
                if response.status_code >= 400:
                    # 409 request-in-progress, 5xx and transport failures have ambiguous outcomes.
                    if response.status_code in {400, 401, 403, 404, 422, 429}:
                        item["status"] = "REJECTED"
                        item["http_status"] = response.status_code
                        self._save()
                        return dict(item)
                    raise ValueError("ambiguous response")
                result = response.json().get("result")
                if (not isinstance(result, dict) or not isinstance(result.get("orderId"), str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", result["orderId"])
                        or result.get("clientOrderId") != request.client_order_id):
                    raise ValueError("invalid create acknowledgment")
                item.update({"order_id": result["orderId"], "status": "PENDING"})
                self.reconciled = False
                self._save()
                return dict(item)
            except (Exception, asyncio.CancelledError) as exc:
                item["status"] = "UNKNOWN"
                self.halt_reasons.add("unknown_order")
                self.reconciled = False
                self._save()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise CapabilityUnavailable("Order outcome is unknown; automatic submissions paused, never resend") from None

    async def cancel_order(self, client_order_id: str) -> dict:
        self._require_live()
        async with self._lock:
            item = self.orders[client_order_id]
            if item["status"] in TERMINAL or item.get("cancel_operation_id"):
                return dict(item)
            if not item.get("order_id"):
                raise CapabilityUnavailable("Cannot cancel an order whose broker identity is unknown")
            if item.get("cancel_requested"):
                raise CapabilityUnavailable("Cancellation outcome is unresolved; refresh original order")
            item["cancel_requested"] = True
            self._save()
            try:
                token = await self.market.access_token()
                await self.market.limiter.wait()
                response = await self.market.client.post(BASE_URL + f"/api/v1/orders/{item['order_id']}/cancel",
                    json={}, headers={"Authorization": "Bearer " + token,
                                     "X-Tossinvest-Account": str(self.market.account_seq)})
                response.raise_for_status()
                operation_id = response.json()["result"]["orderId"]
                if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", operation_id):
                    raise ValueError("invalid cancel acknowledgment")
                item.update({"cancel_operation_id": operation_id, "status": "PENDING_CANCEL"})
                self.reconciled = False
                self._save()
                return dict(item)
            except (Exception, asyncio.CancelledError) as exc:
                self.halt_reasons.add("unknown_order")
                self.reconciled = False
                self._save()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise CapabilityUnavailable("Cancellation outcome is unknown; refresh original order before further orders") from None
