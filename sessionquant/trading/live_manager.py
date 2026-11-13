"""Async broker bridge for the synchronous strategy engine.

Quotes create intents and local stop/target triggers, never simulated fills.
Only broker-reported cumulative quantities/notionals are booked. The account is
the explicitly allocated strategy ledger, NOT a claim about total broker equity.
The runtime must await drain() and persist the engine after processing events.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ..collectors.common import CapabilityUnavailable, DataQualityError
from ..models import Fill, OrderStatus, Quote, utc
from .order_manager import KNOWN_ACTIVE, PaperOrderManager
from .toss_broker import ACTIVE, LimitOrderRequest, TossBroker

STATUS = {"PENDING": OrderStatus.OPEN, "PENDING_CANCEL": OrderStatus.PARTIAL,
          "PARTIAL_FILLED": OrderStatus.PARTIAL, "FILLED": OrderStatus.FILLED,
          "CANCELED": OrderStatus.CANCELLED, "REJECTED": OrderStatus.REJECTED,
          "UNKNOWN": OrderStatus.UNKNOWN, "SUBMITTING": OrderStatus.UNKNOWN}


class LiveOrderManager(PaperOrderManager):
    execution_mode = "live"

    def __init__(self, settings, account, broker: TossBroker, repository=None):
        super().__init__(settings, account, repository)
        self.broker = broker
        self.pending_cancels: set[str] = set()
        self._allocation_verified = False
        self._broker_fee_totals: dict[str, float] = {}
        self._estimated_fee_orders: set[str] = set()
        self._bridge_lock = asyncio.Lock()

    def cancel(self, client_id: str, now: datetime) -> bool:
        utc(now)
        order = self.orders.get(client_id)
        if order is None or order.status not in KNOWN_ACTIVE:
            return False
        if client_id not in self.broker.orders:
            order.status = OrderStatus.CANCELLED
            self._refresh_reservations()
            return True
        self.pending_cancels.add(client_id)
        return True  # Requested, never represented as confirmed cancellation.

    def halt(self, reason: str) -> None:
        # Parent halt emits alerts, but its paper cancellation must never change a
        # live order to CANCELLED before the broker confirms it.
        statuses = {cid: order.status for cid, order in self.orders.items()
                    if cid in self.broker.orders and order.status in KNOWN_ACTIVE}
        super().halt(reason)
        for cid, status in statuses.items():
            self.orders[cid].status = status
            if self.orders[cid].side == "buy":
                self.pending_cancels.add(cid)
        self._refresh_reservations()

    def roll_day(self, now: datetime) -> None:
        previous_day = self._day
        sent = {cid: order.status for cid, order in self.orders.items()
                if cid in self.broker.orders and order.status in KNOWN_ACTIVE}
        super().roll_day(now)  # Shared trading-calendar/loss-latch reset rules.
        if previous_day is not None and previous_day != self._day:
            for cid, status in sent.items():
                self.orders[cid].status = status
                self.pending_cancels.add(cid)
            self._refresh_reservations()

    def on_quote(self, quote: Quote, now: datetime) -> list[Fill]:
        self.roll_day(now)
        for order in list(self.orders.values()):
            if (order.status in KNOWN_ACTIVE
                    and (utc(now) - utc(order.created_at)).total_seconds() >= self.settings.order_ttl_seconds):
                self.cancel(order.client_id, now)
        if self._valid_quote(quote, now):
            self._latest_quotes[quote.symbol] = quote
        return []

    def export_state(self):
        state = super().export_state()
        state.update({"execution_mode": "live", "allocation_verified": self._allocation_verified,
                      "broker_fee_totals": self._broker_fee_totals,
                      "estimated_fee_orders": sorted(self._estimated_fee_orders),
                      "pending_broker_cancels": sorted(self.pending_cancels)})
        return state

    def restore_state(self, state):
        if state.get("execution_mode") != "live":
            raise ValueError("Live execution cannot restore paper-engine state")
        super().restore_state(state)
        self._allocation_verified = bool(state.get("allocation_verified", False))
        self._broker_fee_totals = dict(state.get("broker_fee_totals", {}))
        self._estimated_fee_orders = set(state.get("estimated_fee_orders", []))
        self.pending_cancels = set(state.get("pending_broker_cancels", []))

    def _adjust_fee(self, order, difference: float):
        if abs(difference) < 1e-10:
            return
        self.account.cash -= difference
        symbol = order.symbol
        trade = self._trade_totals.get(symbol)
        if order.side == "buy":
            position = self.account.positions.get(symbol)
            retained = min(1.0, position.qty / order.filled_qty) if position and order.filled_qty else 0.0
            if position:
                position.entry_fee += difference * retained
                position.stop_price, position.target_price = self.costs.exit_levels(
                    position.avg_price, position.qty, position.entry_fee)
            self.account.realized_pnl -= difference * (1 - retained)
            if trade:
                trade["entry_fee"] += difference
                trade["net_pnl"] -= difference * (1 - retained)
        else:
            self.account.realized_pnl -= difference
            if trade:
                trade["exit_fee"] += difference
                trade["net_pnl"] -= difference
        if not trade:
            closed = next((t for t in reversed(self.closed_trades) if t["symbol"] == symbol), None)
            if closed:
                closed["entry_fee" if order.side == "buy" else "exit_fee"] += difference
                closed["net_pnl"] -= difference
                closed["return_pct"] = closed["net_pnl"] / closed["entry_notional"] * 100
                trailing_losses = 0
                for completed in reversed(self.closed_trades):
                    if completed["net_pnl"] >= 0:
                        break
                    trailing_losses += 1
                self.account.consecutive_losses = trailing_losses

    def _book_broker_updates(self):
        for cid, item in self.broker.orders.items():
            order = self.orders.get(cid)
            if order is None:
                self.halt("broker_journal_without_engine_state")
                continue
            total_qty = item.get("filled_quantity", 0)
            if total_qty < order.filled_qty or total_qty > order.qty:
                self.halt("broker_fill_mismatch")
                continue
            fees_known = item.get("commission") is not None and item.get("tax") is not None
            total_amount = float(item.get("filled_amount") or 0)
            if total_qty:
                if fees_known:
                    total_fee = float(item["commission"]) + float(item["tax"])
                    self._estimated_fee_orders.discard(cid)
                else:
                    total_fee = self.costs.charge(order.side, total_amount, total_qty)
                    self._estimated_fee_orders.add(cid)
                prior_fee = self._broker_fee_totals.get(cid, 0.0)
                delta_qty = total_qty - order.filled_qty
                if delta_qty:
                    previous_notional = order.avg_fill_price * order.filled_qty
                    delta_price = (total_amount - previous_notional) / delta_qty
                    if delta_price <= 0:
                        self.halt("broker_fill_mismatch")
                        continue
                    # timestamp is the broker update observation when filledAt is
                    # absent; this never fabricates a historical quote/entry signal.
                    at = utc(item["filled_at"]) if item.get("filled_at") else datetime.now(timezone.utc)
                    delta_fee = max(0.0, total_fee - prior_fee)
                    self._book(order, Fill(f"{cid}-broker-{total_qty}", cid, order.symbol,
                        order.side, delta_qty, delta_price, delta_fee, at))
                    self._adjust_fee(order, total_fee - prior_fee - delta_fee)
                else:
                    self._adjust_fee(order, total_fee - prior_fee)
                self._broker_fee_totals[cid] = total_fee
                if hasattr(self, "_charged_order_fees"):
                    self._charged_order_fees[cid] = total_fee
            order.status = STATUS.get(item["status"], OrderStatus.UNKNOWN)
            if order.status in {OrderStatus.CANCELLED, OrderStatus.FILLED, OrderStatus.REJECTED}:
                self.pending_cancels.discard(cid)
        if self._estimated_fee_orders:
            self.halt("broker_execution_fee_pending")
        else:
            self.halt_reasons.discard("broker_execution_fee_pending")
        self._refresh_reservations()

    async def _synchronize(self) -> bool:
        reconciled = await self.broker.reconcile()
        if not self._allocation_verified:
            if self.orders or self.broker.orders or self.broker.positions:
                self.halt("live_allocation_not_initialized")
                return False
            if not reconciled or self.broker.cash_buying_power is None:
                self.halt("broker_unreconciled")
                return False
            allocation = min(float(self.settings.initial_cash), float(self.broker.cash_buying_power))
            if allocation <= 0:
                self.halt("insufficient_live_allocation")
                return False
            self.account.cash = self.account.equity = self.account.day_start_equity = allocation
            self._allocation_verified = True
        self._book_broker_updates()
        owned = {symbol: position.qty for symbol, position in self.account.positions.items()}
        if not reconciled or owned != self.broker.positions:
            self.halt("broker_unreconciled")
            return False
        self.halt_reasons.discard("broker_unreconciled")
        self.mark_to_market(self._latest_quotes)
        return True

    async def synchronize(self) -> bool:
        async with self._bridge_lock:
            return await self._synchronize()

    async def drain(self) -> bool:
        """Reconcile, request cancellations, then dispatch risk-approved intents."""
        async with self._bridge_lock:
            now = datetime.now(timezone.utc)
            fresh_reconciliation = (self._allocation_verified and self.broker.reconciled
                and not self.broker.halt_reasons and self.broker.reconciled_at is not None
                and (now - self.broker.reconciled_at).total_seconds() <= self.broker.reconciliation_ttl_seconds)
            # Every accepted write invalidates this state. Reuse a still-current
            # read-only reconciliation rather than aging quotes with redundant
            # account requests immediately before each order.
            ready = True if fresh_reconciliation else await self._synchronize()
            for cid in sorted(self.pending_cancels):
                item = self.broker.orders.get(cid)
                if item is None:
                    self.pending_cancels.discard(cid)
                    continue
                try:
                    await self.broker.cancel_order(cid)
                except CapabilityUnavailable:
                    self.halt("broker_unreconciled")
                    ready = False
            if not ready or not self.broker.reconciled:
                return False
            now = datetime.now(timezone.utc)
            for cid, order in list(self.orders.items()):
                if cid in self.broker.orders or order.status not in KNOWN_ACTIVE:
                    continue
                if order.side == "buy" and self.halt_reasons:
                    self.cancel(cid, now)
                    continue
                # Confirm the entry remainder cancellation before an exit; later
                # partial fills can otherwise reopen exposure after a sell.
                if order.side == "sell" and any(
                    i["request"]["symbol"] == order.symbol and i["request"]["side"] == "BUY"
                    and i["status"] in ACTIVE for i in self.broker.orders.values()
                ):
                    continue
                quote = self._latest_quotes.get(order.symbol)
                try:
                    item = await self.broker.submit_limit(LimitOrderRequest(cid, order.symbol,
                        order.side.upper(), order.qty, order.limit_price), quote=quote,
                        risk_approved=True, eligibility_verified=bool(quote and quote.tradable and not quote.halted))
                    order.status = STATUS.get(item["status"], OrderStatus.UNKNOWN)
                except (CapabilityUnavailable, DataQualityError, ValueError):
                    if cid in self.broker.orders:
                        order.status = OrderStatus.UNKNOWN
                        self.halt("unknown_order")
                    else:
                        order.status = OrderStatus.REJECTED
                        self.last_rejection = "broker_preflight_rejected"
                self._refresh_reservations()
                # Every accepted write invalidates account reconciliation.
                if not self.broker.reconciled:
                    break
            return self.broker.reconciled
