from datetime import datetime, timedelta, timezone
import json
import pytest
from pennyquant.config.settings import Settings
from pennyquant.models import Account, OrderStatus, Quote, Signal
from pennyquant.trading.order_manager import PaperOrderManager

T = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)

def quote(t=T, bid=1.998, ask=2.002, size=100, symbol="DEMO", **kwargs):
    return Quote(symbol, t, t, bid, ask, size, size, **kwargs)

def signal(id="entry", t=T, symbol="DEMO"):
    return Signal(id, symbol, "pullback", t, 2.0, "test breakout")

def manager(**kwargs):
    settings = Settings(**kwargs)
    return PaperOrderManager(settings, Account(settings.initial_cash, settings.initial_cash, settings.initial_cash))

def enter(m, size=100):
    order = m.submit(signal(), quote(), T)
    assert order is not None
    fills = m.on_quote(quote(T + timedelta(seconds=1), size=size), T + timedelta(seconds=1))
    assert fills
    return order

def test_same_quote_never_fills_signal():
    m = manager()
    order = m.submit(signal(), quote(), T)
    assert m.on_quote(quote(), T) == []
    assert order.filled_qty == 0
    assert not m.account.positions
    assert m.account.reserved_cash > 0

def test_idempotent_submit_does_not_reserve_twice_and_conflicts_halt():
    m = manager()
    a = m.submit(signal(), quote(), T)
    reserve = m.account.reserved_cash
    assert m.submit(signal(), quote(), T) is a
    assert m.account.entries_today == 1
    assert m.account.reserved_cash == reserve
    assert m.submit(signal(symbol="OTHER"), quote(symbol="OTHER"), T) is None
    assert "idempotency_conflict" in m.halt_reasons
    assert a.status == OrderStatus.CANCELLED

def test_partial_fill_depth_replay_and_restart_preserves_budget():
    m = manager()
    order = m.submit(signal(), quote(), T)
    q = quote(T + timedelta(seconds=1), size=2)
    fills = m.on_quote(q, q.received_at)
    assert sum(f.qty for f in fills) == 2
    assert order.status == OrderStatus.PARTIAL
    assert m.account.positions["DEMO"].qty == 2
    assert m.on_quote(q, q.received_at) == []
    n = manager()
    n.restore_state(json.loads(json.dumps(m.export_state())))
    assert n.on_quote(q, q.received_at) == []
    q2 = quote(T + timedelta(seconds=2), size=100)
    n.on_quote(q2, q2.received_at)
    assert n.orders[order.client_id].status == OrderStatus.FILLED
    assert n.account.positions["DEMO"].qty == order.qty
    assert n.account.reserved_cash == 0
    assert n.account.cash >= 0

def test_unmarketable_limit_does_not_fill_and_expires():
    m = manager()
    order = m.submit(signal(), quote(), T)
    assert m.on_quote(quote(T + timedelta(seconds=1), bid=2.1, ask=2.104), T + timedelta(seconds=1)) == []
    assert m.on_quote(quote(T + timedelta(seconds=31)), T + timedelta(seconds=31)) == []
    assert order.status == OrderStatus.CANCELLED
    assert m.account.reserved_cash == 0
    assert m.account.entries_today == 1

def test_pending_buy_blocks_another_signal():
    m = manager()
    a = m.submit(signal(), quote(), T)
    assert m.submit(signal(id="second"), quote(), T) is None
    assert m.last_rejection == "pending_buy"
    assert len(m.orders) == 1
    m.cancel(a.client_id, T)
    assert m.account.reserved_cash == 0

def test_manual_halt_cancels_entries_but_manages_held_stop():
    m = manager()
    order = enter(m, size=2)
    m.halt("manual")
    assert order.status == OrderStatus.CANCELLED
    assert m.account.positions["DEMO"].qty == 2
    t = T + timedelta(seconds=2)
    stop_quote = quote(t, bid=1.88, ask=1.884)
    sell = m.check_exits(stop_quote, t)
    assert sell.side == "sell" and sell.qty == 2
    assert m.on_quote(stop_quote, t) == []
    t2 = t + timedelta(seconds=1)
    fills = m.on_quote(quote(t2, bid=1.88, ask=1.884), t2)
    assert sum(f.qty for f in fills) == 2
    assert not m.account.positions
    assert m.account.consecutive_losses == 1
    assert len(m.closed_trades) == 1
    assert m.closed_trades[0]["net_pnl"] < 0
    assert "manual" in m.halt_reasons

def test_exit_partial_sizes_and_round_trip_net_cost_accounting():
    m = manager()
    order = enter(m)
    count = order.qty
    t = T + timedelta(seconds=2)
    q = quote(t, bid=2.23, ask=2.234, size=2)
    sell = m.check_exits(q, t)
    assert sell.qty == count
    t2 = t + timedelta(seconds=1)
    m.on_quote(quote(t2, bid=2.23, ask=2.234, size=2), t2)
    assert m.account.positions["DEMO"].qty == count - 2
    assert not m.closed_trades
    t3 = t2 + timedelta(seconds=1)
    m.on_quote(quote(t3, bid=2.23, ask=2.234, size=100), t3)
    assert not m.account.positions
    assert sell.status == OrderStatus.FILLED
    assert len(m.closed_trades) == 1
    trade = m.closed_trades[0]
    assert trade["net_pnl"] == pytest.approx(m.account.cash - 70)
    assert trade["net_pnl"] == pytest.approx(trade["exit_notional"] - trade["entry_notional"] - trade["entry_fee"] - trade["exit_fee"])
    assert all(f.fee > 0 for f in m.fills)

def test_stop_gap_below_limit_stays_unfilled_and_resubmits_after_ttl():
    m = manager()
    enter(m)
    t = T + timedelta(seconds=2)
    sell = m.check_exits(quote(t, bid=1.88, ask=1.884), t)
    t2 = t + timedelta(seconds=1)
    assert m.on_quote(quote(t2, bid=1.6, ask=1.604), t2) == []
    assert m.account.positions
    t3 = t + timedelta(seconds=31)
    m.on_quote(quote(t3, bid=1.6, ask=1.604), t3)
    assert sell.status == OrderStatus.CANCELLED
    new_sell = m.check_exits(quote(t3, bid=1.6, ask=1.604), t3)
    assert new_sell.client_id != sell.client_id
    assert new_sell.limit_price < sell.limit_price

def test_unknown_halts_and_never_reissues_or_cancels_blindly():
    m = manager()
    order = m.submit(signal(), quote(), T)
    m.mark_unknown(order.client_id)
    assert not m.cancel(order.client_id, T)
    assert not m.resume("unknown_order")
    assert m.submit(signal(id="second"), quote(), T) is None
    assert m.submit(signal(), quote(), T) is order
    assert len(m.orders) == 1
    n = manager()
    n.restore_state(m.export_state())
    assert "unknown_order" in n.halt_reasons
    assert n.account.reserved_cash > 0

def test_reconciliation_fault_blocks_entries_and_success_clears_only_fault():
    m = manager()
    enter(m)
    assert not m.reconcile({"DEMO": 99}, set())
    assert "reconciliation_mismatch" in m.halt_reasons
    m.halt("manual")
    assert m.reconcile({"DEMO": m.account.positions["DEMO"].qty}, set())
    assert "reconciliation_mismatch" not in m.halt_reasons
    assert "manual" in m.halt_reasons

def test_roll_day_resets_daily_counters_preserving_faults():
    m = manager()
    enter(m)
    m.halt("manual")
    m.halt("daily_loss")
    m.halt("reconciliation_mismatch")
    m.account.consecutive_losses = 2
    m.roll_day(T + timedelta(days=3))
    assert m.account.entries_today == 0
    assert m.account.consecutive_losses == 0
    assert m.account.realized_pnl == 0
    assert m.account.day_start_equity == m.account.equity
    assert m.account.positions
    assert m.halt_reasons == {"manual", "reconciliation_mismatch"}

def test_daily_equity_loss_halts_entries_but_keeps_position():
    m = manager()
    enter(m)
    t = T + timedelta(seconds=2)
    m.mark_to_market({"DEMO": quote(t, bid=1.7, ask=1.704)})
    assert "daily_loss" in m.halt_reasons
    assert m.account.positions
    assert m.check_exits(quote(t, bid=1.7, ask=1.704), t) is not None


def test_five_pending_or_held_symbols_are_allowed_but_sixth_is_rejected():
    m = manager(initial_cash=1000, risk_per_trade=0.005)
    for index in range(5):
        symbol = f"S{index}"
        assert m.submit(signal(id=symbol, symbol=symbol), quote(symbol=symbol), T) is not None
    assert len(m.orders) == 5
    assert m.submit(signal(id="six", symbol="S5"), quote(symbol="S5"), T) is None
    assert m.last_rejection == "position_limit"
    for index in range(5):
        tick = T + timedelta(seconds=1)
        m.on_quote(quote(tick, symbol=f"S{index}"), tick)
    assert len(m.account.positions) == 5
    assert m.account.cash >= m.account.reserved_cash >= 0


def test_toss_commission_waiver_applies_to_total_order_after_partial_fills_and_restart():
    m = manager(fee_model="toss_us", exit_price_policy="net", slippage_bps=0)
    order = m.submit(signal(), quote(bid=1.998, ask=2.0), T)
    assert order.qty == 6
    for second in (1, 2):
        tick = T + timedelta(seconds=second)
        m.on_quote(quote(tick, bid=1.998, ask=2.0, size=2), tick)
    assert [fill.fee for fill in m.fills] == [0, 0]
    n = manager(fee_model="toss_us", exit_price_policy="net", slippage_bps=0)
    n.restore_state(json.loads(json.dumps(m.export_state())))
    tick = T + timedelta(seconds=3)
    n.on_quote(quote(tick, bid=1.998, ask=2.0, size=2), tick)
    assert n.orders[order.client_id].status == OrderStatus.FILLED
    assert sum(fill.fee for fill in n.fills) == 0.02
    assert n.account.cash == pytest.approx(57.98)


def test_toss_take_profit_requires_net_target_after_fees():
    m = manager(fee_model="toss_us", exit_price_policy="net", slippage_bps=0)
    m.submit(signal(), quote(bid=1.998, ask=2.0), T)
    tick = T + timedelta(seconds=1)
    m.on_quote(quote(tick, bid=1.998, ask=2.0), tick)
    position = m.account.positions["DEMO"]
    assert position.target_price > 2.2
    tick += timedelta(seconds=1)
    assert m.check_exits(quote(tick, bid=2.20, ask=2.204), tick) is None
    assert m.check_exits(quote(tick, bid=position.target_price, ask=position.target_price + .004), tick)


def test_daily_halt_is_latched_notified_once_and_preserved_after_restart():
    alerts = []
    m = manager()
    m.on_alert = alerts.append
    enter(m)
    tick = T + timedelta(seconds=2)
    m.mark_to_market({"DEMO": quote(tick, bid=1.7, ask=1.704)})
    assert "daily_loss" in m.halt_reasons
    assert not m.resume("daily_loss")
    assert len(alerts) == len(m.alerts) == 1
    assert alerts[0]["type"] == "risk_halt" and alerts[0]["details"]["drawdown_pct"] >= .02
    m.mark_to_market({"DEMO": quote(tick, bid=2.1, ask=2.104)})
    assert "daily_loss" in m.halt_reasons
    n = manager()
    n.restore_state(json.loads(json.dumps(m.export_state())))
    assert not n.resume("daily_loss") and len(n.alerts) == 1
    n.mark_to_market({"DEMO": quote(tick, bid=1.7, ask=1.704)})
    assert len(n.alerts) == 1
    assert n.check_exits(quote(tick, bid=1.7, ask=1.704), tick)
    n.roll_day(T + timedelta(days=1))  # Saturday is not a new trading day.
    assert "daily_loss" in n.halt_reasons
    n.roll_day(T + timedelta(days=3))
    assert "daily_loss" not in n.halt_reasons


def test_notification_delivery_failure_does_not_resume_trading():
    m = manager()
    enter(m)
    def broken(alert):
        raise OSError("delivery failure")
    m.on_alert = broken
    tick = T + timedelta(seconds=2)
    m.mark_to_market({"DEMO": quote(tick, bid=1.7, ask=1.704)})
    assert "daily_loss" in m.halt_reasons and len(m.alerts) == 1


def test_fresh_drawdown_cancels_partial_entry_before_adding_more_exposure():
    m = manager(initial_cash=1000, risk_per_trade=.02, daily_loss_pct=.001)
    order = enter(m, size=2)
    assert order.status == OrderStatus.PARTIAL
    tick = T + timedelta(seconds=2)
    fills = m.on_quote(quote(tick, bid=1.4, ask=1.404), tick)
    assert fills == [] and order.status == OrderStatus.CANCELLED
    assert "daily_loss" in m.halt_reasons and m.account.positions["DEMO"].qty == 2

def test_stale_future_halted_and_out_of_order_quotes_cannot_fill():
    m = manager()
    order = m.submit(signal(), quote(), T)
    assert not m.on_quote(quote(T + timedelta(seconds=1)), T)
    assert not m.on_quote(quote(T + timedelta(seconds=1), halted=True), T + timedelta(seconds=1))
    assert not m.on_quote(quote(T + timedelta(seconds=1)), T + timedelta(seconds=10))
    assert order.filled_qty == 0

def test_restored_invalid_cash_or_position_rejected():
    m = manager()
    enter(m)
    state = m.export_state()
    state["account"]["cash"] = -1
    with pytest.raises(ValueError):
        manager().restore_state(state)


def test_paper_restore_rejects_live_ledger_but_accepts_legacy_paper_state():
    m = manager()
    state = m.export_state()
    assert state["execution_mode"] == "paper"
    state["execution_mode"] = "live"
    with pytest.raises(ValueError, match="execution mode"):
        manager().restore_state(state)
    state.pop("execution_mode")
    n = manager()
    n.restore_state(state)
    assert n.export_state()["execution_mode"] == "paper"


def test_liquidity_budget_bounded_after_100_quotes_and_latest_replay_after_restart():
    m = manager()
    entry = m.submit(signal(), quote(), T)
    for second in range(1, 101):
        tick = T + timedelta(seconds=second)
        m.on_quote(quote(tick), tick)
    assert entry.status == OrderStatus.FILLED
    assert len(m.export_state()["quote_liquidity"]) == 1
    # Independent symbol budgets remain available alongside the latest DEMO tick.
    tick = T + timedelta(seconds=100)
    m.on_quote(quote(tick, symbol="OTHER"), tick)
    assert len(m.export_state()["quote_liquidity"]) == 2
    trigger_at = T + timedelta(seconds=101)
    exit_order = m.check_exits(quote(trigger_at, bid=2.23, ask=2.234), trigger_at)
    latest_at = T + timedelta(seconds=102)
    latest = quote(latest_at, bid=2.23, ask=2.234, size=2)
    assert sum(fill.qty for fill in m.on_quote(latest, latest_at)) == 2
    assert exit_order.status == OrderStatus.PARTIAL
    n = manager()
    n.restore_state(json.loads(json.dumps(m.export_state())))
    remaining = n.account.positions["DEMO"].qty
    assert len(n.export_state()["quote_liquidity"]) == 2
    assert n.on_quote(latest, latest_at) == []
    # This older quote is fresh by wall time and post-order, but already passed
    # market chronology, so pruning its former budget cannot permit new fills.
    older_at = T + timedelta(seconds=101, milliseconds=500)
    assert n.on_quote(quote(older_at, bid=2.23, ask=2.234), latest_at) == []
    assert n.account.positions["DEMO"].qty == remaining
    assert len(n.export_state()["quote_liquidity"]) == 2
