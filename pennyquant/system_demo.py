"""Offline integration fixture, visibly synthetic and incapable of live orders."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .config.settings import Settings
from .database.repository import Repository
from .demo import demo_events
from .engine import EventEngine
from .events import Event
from .news.analysis import ExtractedEvent, NewsAnalysis, event_digest


def write_system_demo(output: Path, settings: Settings, strategy: str = "pullback") -> Path:
    output = Path(output)
    db = output / "system-demo.db"
    if db.exists():
        raise ValueError("System demo DB exists; choose a fresh --out folder")
    settings = replace(settings, news_policy="primary", fee_model="toss_us", exit_price_policy="net")
    repo = Repository(db)
    try:
        repo.set_control("runtime_kind", "market_pipeline")
        repo.set_control("pipeline_mode", "paper")
        repo.set_control("source", {"synthetic": True, "source_label": "SYNTHETIC_OFFLINE_FIXTURE"})
        engine = EventEngine(settings, strategy, repo)
        for event in demo_events():
            if event.kind == "news":
                news = replace(event.data, id="rss:" + event.data.id, real_time=False,
                               source="SYNTHETIC company RSS fixture", body=event.data.title)
                analysis = NewsAnalysis(ticker="DEMO", event=news, sentiment="POSITIVE",
                    events=(ExtractedEvent("FDA_APPROVAL", "CONFIRMED", "POSITIVE", "POSITIVE",
                                           "HIGH", False, news.title),),
                    confidence=1.0, requires_verification=False, reason="합성 데이터의 고정 분석 결과 · 실제 LLM 호출 아님",
                    primary_verified=True, entry_eligible=True, source_kind="rss", model="OFFLINE_FIXTURE",
                    analyzed_at=news.received_at, input_digest=event_digest(news, "DEMO"))
                engine.register_analysis(analysis)
                event = Event("news", news)
            engine.process(event)
        quote = engine.quotes.get("DEMO")
        repo.record("candidates", {"id": "DEMO", "symbol": "DEMO", "source": "SYNTHETIC_FIXTURE",
            "triggers": ["volume_spike", "price_surge"], "last_price": quote.ask if quote else None,
            "readiness": "WATCH", "blockers": ["synthetic_fixture"], "received_at": engine.last_time})
        repo.set_control("pipeline_status", {"status": "stopped", "mode": "paper", "synthetic": True,
            "strategy": strategy, "max_positions": settings.max_positions, "daily_loss_pct": settings.daily_loss_pct,
            "halt_reasons": sorted(engine.manager.halt_reasons), "last_event": engine.last_time,
            "last_cycle_at": engine.last_time, "active_symbols": ["DEMO"], "live_orders_enabled": False,
            "fee_summary": "토스 미국주식 비용 추정 · 합성 체결", "llm_model": "OFFLINE_FIXTURE"})
        return db
    finally:
        repo.close()
