from __future__ import annotations

from collections import defaultdict
import math

from ..models import OrderStatus


def summarize(manager, equity_curve: list[dict], initial_cash: float) -> dict:
    trades = manager.closed_trades
    pnl = [float(t.get("net_pnl", t.get("pnl", 0))) for t in trades]
    profit = sum(max(v, 0) for v in pnl)
    loss = -sum(min(v, 0) for v in pnl)
    peak = initial_cash
    mdd = 0.0
    for point in equity_curve:
        equity = float(point["equity"])
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak if peak > 0 else 0)
    streak = max_streak = 0
    for value in pnl:
        streak = streak + 1 if value < 0 else 0
        max_streak = max(max_streak, streak)
    orders = list(manager.orders.values())
    buys = [order for order in orders if order.side == "buy"]
    no_fill = sum(o.filled_qty == 0 for o in buys)
    entries = [f for f in manager.fills if f.side == "buy"]
    slippages = []
    for fill in entries:
        order = manager.orders[fill.client_id]
        # Limit price is a cap, not a pre-trade arrival quote. Do not call limit-to-fill difference slippage.
        if hasattr(manager, "fill_slippage_bps"):
            slippages.extend(manager.fill_slippage_bps)
            break
    return {
        "trades": len(pnl), "net_pnl": round(manager.account.equity - initial_cash, 6),
        "closed_net_pnl": round(sum(pnl), 6),
        "return_pct": round(100 * (manager.account.equity / initial_cash - 1), 6),
        "win_rate": sum(p > 0 for p in pnl) / len(pnl) if pnl else 0.0,
        "profit_factor": round(profit / loss, 6) if loss else None,
        "profit_factor_note": "no losing trades / undefined" if not loss else "net closed trades",
        "max_drawdown_pct": round(mdd * 100, 6),
        "avg_trade_pnl": round(sum(pnl) / len(pnl), 6) if pnl else 0.0,
        "max_consecutive_losses": max_streak,
        "unfilled_order_rate": no_fill / len(buys) if buys else 0.0,
        "partial_order_rate": sum(0 < o.filled_qty < o.qty for o in buys) / len(buys) if buys else 0.0,
        "slippage_bps": manager.settings.slippage_bps,
        "slippage_note": "configured adverse execution assumption, not measured live slippage",
        "final_equity": round(manager.account.equity, 6),
        "cash": round(manager.account.cash, 6),
        "fees": round(sum(f.fee for f in manager.fills), 6),
        "buy_orders": len(buys), "fills": len(manager.fills),
        "open_positions": len(manager.account.positions),
        "open_orders": sum(o.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.UNKNOWN) for o in orders),
        "capital_efficiency": round(sum(pnl) / initial_cash, 6),
    }


def validate_report(report: dict) -> None:
    def check(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite report value")
        if isinstance(value, dict):
            for item in value.values(): check(item)
        elif isinstance(value, list):
            for item in value: check(item)
    check(report)
