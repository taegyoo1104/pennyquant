import pytest

from sessionquant.config.settings import Settings
from sessionquant.trading.costs import FeeSchedule


def schedule(**kwargs):
    return FeeSchedule(Settings(fee_model="toss_us", exit_price_policy="net", **kwargs))


def test_toss_standard_order_commission_threshold_and_sell_regulatory_minimum():
    costs = schedule()
    assert costs.charge("buy", 10, 5) == 0
    assert costs.charge("sell", 10, 5) == 0.01
    assert costs.charge("buy", 10.01, 5) == 0.02
    assert costs.charge("buy", 100, 50) == 0.10
    assert costs.charge("sell", 100, 50) == 0.11
    assert costs.charge("sell", 1000, 500) == 1.03


def test_small_order_waiver_does_not_remove_sec_fee_or_configured_share_fee():
    costs = schedule(sell_per_share_fee=0.000195, sell_per_share_max=9.79)
    assert costs.charge("sell", 8, 4) == 0.02
    assert costs.charge("sell", 1_000_000, 100_000) == pytest.approx(1000 + 20.60 + 9.79)


def test_net_targets_and_breakeven_cover_entry_exit_fees_and_slippage():
    costs = schedule(slippage_bps=10)
    qty, price = 50, 2.0
    entry_fee = costs.charge("buy", qty * price, qty)
    basis = qty * price + entry_fee
    stop, target = costs.exit_levels(price, qty, entry_fee)
    assert stop > price * 0.95
    assert target > price * 1.10
    assert costs.proceeds(target, qty) >= basis * 1.10
    assert costs.proceeds(target - 0.01, qty) < basis * 1.10
    assert costs.proceeds(stop, qty) >= basis * 0.95
    breakeven = costs.breakeven_bid(price, qty, entry_fee)
    assert costs.proceeds(breakeven, qty) >= basis
    assert costs.proceeds(breakeven - 0.01, qty) < basis


def test_legacy_costs_and_gross_levels_remain_reproducible():
    costs = FeeSchedule(Settings())
    assert costs.charge("buy", 12.024, 6) == pytest.approx(0.012024)
    assert costs.exit_levels(2, 6, 0.012) == pytest.approx((1.9, 2.2))


@pytest.mark.parametrize("kwargs", [{"slippage_bps": 10000}, {"commission_sell_bps": 9999,
                                                                   "sell_regulatory_bps": 1},
                                    {"max_positions": 6}, {"fee_model": "unknown"}])
def test_unsafe_or_unknown_cost_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_pipeline_example_settings_enable_toss_net_costs_and_five_positions():
    from sessionquant.config.settings import load_settings
    from pathlib import Path
    settings = load_settings(Path(__file__).parents[1] / "sessionquant/config/strategy.yaml")
    assert settings.max_positions == 5 and settings.max_entries >= 5
    assert settings.fee_model == "toss_us" and settings.exit_price_policy == "net"
    assert settings.news_policy == "primary"
