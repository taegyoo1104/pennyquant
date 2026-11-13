"""Conservative USD order-cost estimates and net-return exit levels.

Toss API public schedule, checked 2026-11-13:
https://home.tossinvest.com/ko/open-api
0.1% each side, commission waived when the TOTAL filled order value <= $10.
https://corp.tossinvest.com/ko/faq (search: 수수료)
US sells add SEC fee 0.00206%, minimum $0.01. These estimates round charges UP
to cents; actual broker rounding, account discounts and future rate changes can
differ. TAF is configurable rather than assumed to be an extra Toss charge.
Currency conversion and annual capital-gains tax are not per-order charges.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import math

from ..config.settings import Settings


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


@dataclass(frozen=True)
class FeeSchedule:
    settings: Settings

    def charge(self, side: str, notional: float, qty: int) -> float:
        if side not in {"buy", "sell"}:
            raise ValueError("fee side must be buy or sell")
        if not math.isfinite(notional) or notional < 0 or not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
            raise ValueError("fee inputs must be finite nonnegative notional and whole quantity")
        if notional == 0 or qty == 0:
            return 0.0
        s = self.settings
        if s.fee_model == "legacy":
            return round(notional * s.fee_bps / 10_000, 10)
        amount = Decimal(str(round(notional, 8)))
        bps = s.commission_buy_bps if side == "buy" else s.commission_sell_bps
        commission = Decimal("0") if amount <= Decimal(str(s.commission_free_notional)) else _money(amount * Decimal(str(bps)) / 10000)
        regulatory = Decimal("0")
        if side == "sell":
            if s.sell_regulatory_bps or s.sell_regulatory_min:
                regulatory += _money(max(amount * Decimal(str(s.sell_regulatory_bps)) / 10000,
                                         Decimal(str(s.sell_regulatory_min))))
            if s.sell_per_share_fee:
                per_share = Decimal(qty) * Decimal(str(s.sell_per_share_fee))
                if s.sell_per_share_max:
                    per_share = min(per_share, Decimal(str(s.sell_per_share_max)))
                regulatory += _money(per_share)
        return float(commission + regulatory)

    def proceeds(self, bid: float, qty: int) -> float:
        price = bid * (1 - self.settings.slippage_bps / 10_000)
        notional = price * qty
        return notional - self.charge("sell", notional, qty)

    def exit_levels(self, entry_price: float, qty: int, entry_fee: float) -> tuple[float, float]:
        """Bid triggers, with fees and modeled adverse sell slippage deducted.

        A trigger is not a guaranteed execution price: gaps/halts can exceed the
        stop budget. The net thresholds are computed from actual filled costs.
        """
        s = self.settings
        if s.exit_price_policy == "gross":
            return entry_price * (1 - s.stop_pct), entry_price * (1 + s.take_profit_pct)
        if (not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0
                or not math.isfinite(entry_price) or entry_price <= 0
                or not math.isfinite(entry_fee) or entry_fee < 0):
            raise ValueError("positive entry price and quantity required")
        basis = entry_price * qty + entry_fee

        def solve(required: float) -> float:
            low, high = 0.0, max(entry_price * 2, (required + 1) / qty * 2)
            while self.proceeds(high, qty) < required:
                high *= 2
            for _ in range(80):
                mid = (low + high) / 2
                if self.proceeds(mid, qty) < required:
                    low = mid
                else:
                    high = mid
            # Net loss may trigger earlier, net profit only after costs are met.
            return float(_money(Decimal(str(high))))

        return solve(basis * (1 - s.stop_pct)), solve(basis * (1 + s.take_profit_pct))

    def breakeven_bid(self, entry_price: float, qty: int, entry_fee: float | None = None) -> float:
        """Smallest cent bid whose estimated liquidation covers entry cost."""
        if (not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0
                or not math.isfinite(entry_price) or entry_price <= 0
                or (entry_fee is not None and (not math.isfinite(entry_fee) or entry_fee < 0))):
            raise ValueError("finite positive entry price, whole quantity and nonnegative fee required")
        fee = self.charge("buy", entry_price * qty, qty) if entry_fee is None else entry_fee
        required = entry_price * qty + fee
        low, high = entry_price, entry_price * 2 + 1
        for _ in range(80):
            mid = (low + high) / 2
            if self.proceeds(mid, qty) < required:
                low = mid
            else:
                high = mid
        return float(_money(Decimal(str(high))))
