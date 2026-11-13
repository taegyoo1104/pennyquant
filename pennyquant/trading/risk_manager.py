"""Fail-closed entry checks and whole-share sizing for the paper broker."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from functools import lru_cache
import math
from zoneinfo import ZoneInfo

from ..config.settings import Settings
from ..models import Account, Quote, RiskDecision, Signal, utc
from .costs import FeeSchedule

NY = ZoneInfo("America/New_York")
ALLOWED_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "NYSEAMERICAN", "NYSE AMERICAN", "ARCA", "NYSEARCA"}


@lru_cache(maxsize=8)
def _calendar(year: int):
    # Bound calendars explicitly: exchange_calendars' implicit current-date
    # range must not silently invalidate old backtests or a future session.
    import exchange_calendars as xcals
    return xcals.get_calendar("XNYS", start=f"{year}-01-01", end=f"{year}-12-31")


def is_regular_session(now: datetime) -> bool:
    """Use XNYS holidays, DST and early closes; unavailable calendar => closed."""
    try:
        moment = utc(now)
        day = moment.astimezone(NY).date()
        calendar = _calendar(day.year)
        if not calendar.is_session(str(day)):
            return False
        return calendar.session_open(str(day)).to_pydatetime() <= moment < calendar.session_close(str(day)).to_pydatetime()
    except (ValueError, TypeError, ImportError, KeyError, OverflowError):
        return False


def buy_limit(ask: float, slippage_bps: float) -> float:
    """Marketable penny-increment limit, capped by explicit entry sizing."""
    value = Decimal(str(ask)) * (Decimal("1") + Decimal(str(slippage_bps)) / Decimal("10000"))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.costs = FeeSchedule(settings)

    def evaluate(self, signal: Signal, quote: Quote, account: Account, now: datetime, *, halted: bool = False) -> RiskDecision:
        def reject(reason: str) -> RiskDecision:
            return RiskDecision(False, reason)

        s = self.settings
        if halted:
            return reject("entry_halted")
        try:
            now = utc(now)
            event_at, receipt_at, signal_at = utc(quote.timestamp), utc(quote.received_at), utc(signal.timestamp)
        except (ValueError, TypeError, AttributeError):
            return reject("invalid_timestamp")
        if signal.symbol != quote.symbol or not signal.symbol:
            return reject("symbol_mismatch")
        if signal_at > now or event_at > receipt_at or receipt_at > now:
            return reject("future_or_inconsistent_timestamp")
        if (now - event_at).total_seconds() > s.max_quote_age_seconds or (now - receipt_at).total_seconds() > s.max_quote_age_seconds:
            return reject("stale_quote")
        if (now - signal_at).total_seconds() > s.max_quote_age_seconds:
            return reject("stale_signal")
        if s.regular_hours_only and not is_regular_session(now):
            return reject("outside_regular_session")
        if not quote.tradable or quote.halted or quote.exchange.upper() not in ALLOWED_EXCHANGES:
            return reject("instrument_not_tradable")
        if quote.bid_size <= 0 or quote.ask_size <= 0:
            return reject("empty_quote_size")
        if quote.spread_pct > s.max_spread_pct:
            return reject("spread_too_wide")
        if not s.min_price <= quote.ask <= s.max_price:
            return reject("price_out_of_range")
        if not math.isfinite(signal.reference_price) or signal.reference_price <= 0:
            return reject("invalid_signal_price")
        money = (account.cash, account.equity, account.day_start_equity, account.reserved_cash, account.realized_pnl)
        if not all(math.isfinite(value) for value in money) or account.cash < 0 or account.reserved_cash < 0 or account.day_start_equity <= 0 or account.equity <= 0:
            return reject("invalid_account")
        if account.equity <= account.day_start_equity * (1 - s.daily_loss_pct) + 1e-10:
            return reject("daily_loss_limit")
        if account.entries_today >= s.max_entries:
            return reject("daily_entry_limit")
        if account.consecutive_losses >= s.max_consecutive_losses:
            return reject("consecutive_loss_limit")
        if signal.symbol in account.positions:
            return reject("averaging_prohibited")
        if len(account.positions) >= s.max_positions:
            return reject("position_limit")
        available = account.cash - account.reserved_cash
        limit = buy_limit(quote.ask, s.slippage_bps)
        fee = (s.fee_bps if s.fee_model == "legacy" else s.commission_buy_bps) / 10_000
        # Stop distance plus round-trip fees, an extra adverse exit movement,
        # and the observed spread. The limit already budgets entry slippage.
        risk_per_share = limit * (s.stop_pct + 2 * fee + s.slippage_bps / 10_000) + quote.ask - quote.bid
        risk_budget = min(account.equity, account.day_start_equity) * s.risk_per_trade
        quantity = min(math.floor((available + 1e-10) / (limit * (1 + fee))), math.floor((risk_budget + 1e-10) / risk_per_share))
        if s.fee_model != "legacy" or s.exit_price_policy == "net":
            while quantity > 0:
                entry_fee = self.costs.charge("buy", quantity * limit, quantity)
                stop, target = self.costs.exit_levels(limit, quantity, entry_fee)
                modeled_loss = quantity * limit + entry_fee - self.costs.proceeds(stop, quantity)
                modeled_loss += quantity * (quote.ask - quote.bid)
                if quantity * limit + entry_fee <= available + 1e-10 and modeled_loss <= risk_budget + 1e-10:
                    return RiskDecision(True, "approved", quantity, limit, stop, target)
                quantity -= 1
        if quantity <= 0:
            return reject("insufficient_cash_or_risk_budget")
        return RiskDecision(True, "approved", quantity, limit, limit * (1 - s.stop_pct), limit * (1 + s.take_profit_pct))
