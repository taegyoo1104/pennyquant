from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
import math
import yaml


@dataclass(frozen=True)
class Settings:
    initial_cash: float = 70.0
    risk_per_trade: float = 0.01
    stop_pct: float = 0.05
    take_profit_pct: float = 0.10
    daily_loss_pct: float = 0.02
    max_entries: int = 2
    max_positions: int = 1
    max_consecutive_losses: int = 2
    max_spread_pct: float = 0.005
    max_quote_age_seconds: int = 5
    max_news_age_hours: int = 24
    max_news_delay_seconds: int = 120
    max_candidates: int = 20
    min_price: float = 1.0
    max_price: float = 5.0
    min_gain_pct: float = 0.10
    min_relative_volume: float = 3.0
    min_dollar_volume: float = 2_000_000.0
    max_drawdown_from_high: float = 0.12
    fee_bps: float = 10.0
    slippage_bps: float = 10.0
    order_ttl_seconds: int = 30
    require_realtime_news: bool = True
    regular_hours_only: bool = True
    mode: str = "paper"

    def __post_init__(self):
        defaults = {item.name: item.default for item in fields(self)}
        for item in fields(self):
            value = getattr(self, item.name)
            expected = type(defaults[item.name])
            if expected is bool and type(value) is not bool:
                raise ValueError(f"{item.name} must be boolean")
            if expected is int and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"{item.name} must be an integer")
            if expected is float and (not isinstance(value, (float, int)) or isinstance(value, bool)):
                raise ValueError(f"{item.name} must be numeric")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"invalid setting: {item.name}")
        for name in ("risk_per_trade", "stop_pct", "take_profit_pct", "daily_loss_pct", "max_spread_pct"):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.initial_cash <= 0 or self.min_price <= 0 or self.max_price < self.min_price:
            raise ValueError("invalid capital or price range")
        if any(getattr(self, name) <= 0 for name in ("max_entries", "max_positions", "max_candidates", "order_ttl_seconds", "max_quote_age_seconds")):
            raise ValueError("entry, position, candidate, TTL and freshness limits must be positive")
        if self.mode != "paper":
            raise ValueError("This MVP supports paper mode only; live adapter requires separate verified implementation and approval")


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        return Settings()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("settings must be a mapping")
    allowed = {item.name for item in fields(Settings)}
    if set(data) - allowed:
        raise ValueError(f"unknown settings: {sorted(set(data) - allowed)}")
    return Settings(**data)
