from datetime import datetime, timedelta, timezone

import pytest

from pennyquant.collectors.market_scanner import MarketScanner, candle_observation, parse_rankings
from pennyquant.models import Bar
from pennyquant.pipeline_config import PipelineConfig, load_pipeline_config


NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)


def row(symbol="TEST", **updates):
    return {"symbol": symbol, "currency": "USD", "rank": 1,
            "price": {"lastPrice": "2", "basePrice": "1.8", "changeRate": "0.11"},
            "tradingVolume": "200000", "tradingAmount": "3000000", **updates}


def payload(rows=None, **updates):
    return {"rankedAt": NOW.isoformat(), "rankings": [row()] if rows is None else rows, **updates}


def test_rankings_are_watch_candidates_without_fabricated_tradability_or_rvol():
    candidate = parse_rankings(payload(), "TOP_GAINERS", NOW, PipelineConfig())[0].to_dict()
    assert candidate["symbol"] == "TEST" and candidate["triggers"] == ["price_rise"]
    assert candidate["readiness"] == "WATCH"
    assert candidate["blockers"] == ["verified_market_snapshot_required", "primary_news_analysis_required"]
    assert "relative_volume" not in candidate and "tradable" not in candidate and "prev_close" not in candidate


def test_declines_are_watch_triggers_and_numbers_are_not_coerced_from_missing_or_nan():
    rows = [row("FALL", price={"lastPrice": "2", "changeRate": "-0.2"}),
            row("INVALID", tradingVolume="NaN"), row("EXPENSIVE", price={"lastPrice": "100"}),
            row("NOCURRENCY", currency="KRW")]
    result = parse_rankings(payload(rows), "TOP_LOSERS", NOW, PipelineConfig())
    assert len(result) == 1 and result[0].symbol == "FALL"
    assert result[0].triggers == ["price_fall_watch"] and result[0].readiness == "WATCH"


@pytest.mark.parametrize("ranked_at,reason", [(None, "ranking_time_unknown"),
    ((NOW - timedelta(minutes=10)).isoformat(), "ranking_stale_or_future"),
    ((NOW + timedelta(seconds=1)).isoformat(), "ranking_stale_or_future")])
def test_unknown_or_stale_rank_time_remains_a_blocker(ranked_at, reason):
    assert reason in parse_rankings(payload(rankedAt=ranked_at), "TOP_GAINERS", NOW, PipelineConfig())[0].blockers


async def test_discovery_combines_official_rankings_and_caps_active_candidates():
    class FakeMarket:
        async def rankings(self, **kwargs):
            return payload([row(f"T{index}") for index in range(10)])
    scanner = MarketScanner(FakeMarket(), PipelineConfig(max_candidates=3), clock=lambda: NOW)
    candidates = await scanner.discover(NOW)
    assert len(candidates) == 3
    assert candidates[0].triggers == ["price_rise", "high_market_volume", "high_market_turnover"]
    assert all(candidate.first_seen_at == NOW for candidate in await scanner.discover(NOW + timedelta(seconds=10)))


def test_short_window_volume_ratio_is_separate_from_historical_rvol_and_requires_contiguous_bars():
    bars = [Bar("TEST", NOW - timedelta(minutes=5-index), NOW, 2, 2, 2, 2, 100 if index < 5 else 400)
            for index in range(6)]
    observation = candle_observation(bars, PipelineConfig())
    assert observation == {"short_volume_ratio": 4.0, "volume_spike": True}
    assert candle_observation(bars[1:], PipelineConfig())["short_volume_ratio"] is None
    assert "relative_volume" not in observation


def test_pipeline_config_paths_resolve_against_yaml_and_reject_secret_or_ambiguous_fields(tmp_path):
    path = tmp_path / "pipeline.yaml"
    path.write_text("db_path: data/system.db\ninput_event_path: feed/events.jsonl\ncompanies: {}\n", encoding="utf-8")
    config = load_pipeline_config(path)
    assert config.db_path == tmp_path / "data/system.db"
    assert config.input_event_path == tmp_path / "feed/events.jsonl"
    path.write_text("OPENAI_API_KEY: PRIVATE\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown"):
        load_pipeline_config(path)
    with pytest.raises(ValueError, match="input_event_path"):
        PipelineConfig(discover_with_toss=False)
    with pytest.raises(ValueError, match="max_candidates"):
        PipelineConfig(max_candidates=21)
