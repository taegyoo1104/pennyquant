"""Configuration for the market-first, primary-news pipeline.

Relative paths are resolved against the configuration file, not the terminal's
working directory. Credentials are deliberately absent from this schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import math
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .models import symbol_valid


RANKING_TYPES = ("TOP_GAINERS", "TOP_LOSERS", "MARKET_TRADING_VOLUME", "MARKET_TRADING_AMOUNT")


@dataclass(frozen=True)
class PipelineConfig:
    db_path: Path = Path("data/system.db")
    strategy: str = "pullback"
    mode: str = "paper"
    poll_seconds: float = 10.0
    rankings_refresh_seconds: float = 60.0
    news_refresh_seconds: float = 60.0
    rankings_max_age_seconds: float = 180.0
    max_candidates: int = 20
    ranking_count: int = 50
    candle_symbols_per_cycle: int = 2
    quote_candidates_per_cycle: int = 2
    price_min: float = 1.0
    price_max: float = 5.0
    min_abs_change: float = 0.05
    min_market_volume: float = 100_000.0
    min_market_amount: float = 2_000_000.0
    volume_spike_ratio: float = 3.0
    ranking_types: tuple[str, ...] = RANKING_TYPES
    symbols: tuple[str, ...] = ()
    companies: dict = field(default_factory=dict)
    input_event_path: Path | None = None
    include_alpaca: bool = True
    discover_with_toss: bool = True

    def __post_init__(self):
        if self.mode not in {"paper", "live"}:
            raise ValueError("pipeline mode must be paper or live")
        if self.strategy not in {"momentum", "pullback", "gap_breakout", "vwap"}:
            raise ValueError("unknown pipeline strategy")
        for name in ("poll_seconds", "rankings_refresh_seconds", "news_refresh_seconds", "rankings_max_age_seconds",
                     "price_min", "price_max", "min_abs_change", "min_market_volume", "min_market_amount", "volume_spike_ratio"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.poll_seconds < 1 or self.rankings_refresh_seconds < 5 or self.news_refresh_seconds < 5:
            raise ValueError("poll_seconds must be >=1; source refresh intervals must be >=5")
        if self.price_max < self.price_min:
            raise ValueError("price_max must be at least price_min")
        for name, maximum in (("max_candidates", 20), ("ranking_count", 100),
                              ("candle_symbols_per_cycle", 20), ("quote_candidates_per_cycle", 20)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        if type(self.include_alpaca) is not bool or type(self.discover_with_toss) is not bool:
            raise ValueError("include_alpaca and discover_with_toss must be boolean")
        if not self.discover_with_toss and self.input_event_path is None:
            raise ValueError("input_event_path is required when discover_with_toss is false")
        if not self.ranking_types or any(item not in RANKING_TYPES for item in self.ranking_types):
            raise ValueError("unsupported ranking_types")
        if len(self.symbols) > 20:
            raise ValueError("at most 20 configured symbols are supported")
        for symbol in self.symbols:
            symbol_valid(symbol)
        if not isinstance(self.companies, dict):
            raise ValueError("companies must map exact tickers to company-specific sources")
        for symbol, company in self.companies.items():
            symbol_valid(symbol)
            if not isinstance(company, dict) or set(company) - {"symbol", "cik", "company_name", "rss_urls", "rss_verified"}:
                raise ValueError("unsupported company source fields")
            if company.get("symbol", symbol) != symbol:
                raise ValueError("company symbol must match its mapping key")
            cik = company.get("cik", "")
            if cik and (not isinstance(cik, (str, int)) or isinstance(cik, bool) or not str(cik).isdigit()
                        or not 0 < int(cik) < 10**10):
                raise ValueError("company cik must contain 1-10 digits")
            if not isinstance(company.get("company_name", ""), str):
                raise ValueError("company_name must be text")
            urls = company.get("rss_urls", [])
            if not isinstance(urls, (list, tuple)) or any(not isinstance(url, str) or urlsplit(url).scheme != "https"
                                                        or not urlsplit(url).hostname or urlsplit(url).username
                                                        or urlsplit(url).password for url in urls):
                raise ValueError("rss_urls must contain HTTPS company feed URLs")
            if type(company.get("rss_verified", False)) is not bool:
                raise ValueError("rss_verified must be boolean")


def load_pipeline_config(path: str | Path) -> PipelineConfig:
    path = Path(path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict) or set(payload) - {item.name for item in fields(PipelineConfig)}:
        raise ValueError("unknown pipeline configuration fields")
    for name in ("symbols", "ranking_types"):
        if name in payload:
            if not isinstance(payload[name], list):
                raise ValueError(f"{name} must be an array")
            payload[name] = tuple(payload[name])
    for name in ("db_path", "input_event_path"):
        if payload.get(name) is not None:
            if not isinstance(payload[name], str) or not payload[name].strip():
                raise ValueError(f"{name} must be a nonempty path")
            value = Path(payload[name])
            payload[name] = value if value.is_absolute() else path.resolve().parent / value
    if "db_path" not in payload:
        payload["db_path"] = path.resolve().parent / "data/system.db"
    return PipelineConfig(**payload)
