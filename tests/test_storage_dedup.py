"""Storage identities deduplicate refreshes without changing event availability."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sessionquant.config.settings import Settings
from sessionquant.database.repository import Repository
from sessionquant.engine import EventEngine
from sessionquant.models import NewsEvent
from sessionquant.news.analysis import ExtractedEvent, NewsAnalysis, event_digest
from sessionquant.news.event_filter import positive_event

NOW = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)


def event(identity="one", *, published=NOW - timedelta(minutes=1), received=NOW, body=None):
    return NewsEvent(f"rss:{identity}", ("TEST",), "Issuer confirms a binding contract", published, received,
                     "Verified issuer IR", "https://issuer.example/release",
                     body or "The issuer executed a binding contract for twelve million dollars.",
                     verified=True, real_time=False)


def result(item=None, **changes):
    item = item or event()
    base = NewsAnalysis("TEST", item, model="gpt-4.1-mini", source_kind="rss", primary_verified=True,
                        analyzed_at=item.received_at, input_digest=event_digest(item, "TEST"),
                        error="llm_key_missing", reason="API key required")
    return replace(base, **changes)


def test_refresh_clocks_receipts_and_usage_do_not_create_new_logical_analysis():
    first = result()
    later_event = replace(first.event, received_at=NOW + timedelta(minutes=5))
    later = replace(first, event=later_event, analyzed_at=later_event.received_at,
                    latency_ms=300, input_tokens=120, output_tokens=90)
    assert first.to_dict()["analysis_id"] == later.to_dict()["analysis_id"]
    assert first.to_dict()["analyzed_at"] != later.to_dict()["analyzed_at"]
    assert first.to_dict()["event"]["received_at"] != later.to_dict()["event"]["received_at"]


@pytest.mark.parametrize("changes", [
    {"error": "llm_http_429"}, {"sentiment": "POSITIVE", "entry_eligible": True, "error": ""},
    {"confidence": 0.8}, {"reason": "Newly verified facts"}, {"model": "another-model"},
    {"prompt_version": "new-prompt"}, {"dilution_risk": True},
])
def test_real_recovery_or_changed_decision_gets_a_new_analysis_identity(changes):
    first = result()
    assert first.to_dict()["analysis_id"] != replace(first, **changes).to_dict()["analysis_id"]


def test_identity_binds_full_original_article_even_without_optional_input_digest():
    first = result(input_digest="")
    revised = replace(first, event=replace(first.event, body="Revised original financial facts."))
    assert first.to_dict()["analysis_id"] != revised.to_dict()["analysis_id"]


def test_repeated_unknown_analysis_uses_one_archive_record_and_one_active_body(tmp_path):
    repo = Repository(tmp_path / "dedup.db")
    try:
        engine = EventEngine(Settings(news_policy="primary"), repository=repo)
        first = result(event(body="x" * 100000))
        for minute in range(5):
            received = NOW + timedelta(minutes=minute)
            repeated = replace(first, event=replace(first.event, received_at=received), analyzed_at=received)
            engine.register_analysis(repeated, now=received)
        assert len(repo.all("news_analysis")) == 1
        assert len(engine.news) == len(engine.analyses) == 1
        assert next(iter(engine.news.values())).received_at == NOW
        assert next(iter(engine.analyses.values()))["analyzed_at"] == NOW.isoformat()
        assert repo.get_control("engine_context")["news"][0]["received_at"] == NOW.isoformat()
    finally:
        repo.close()


def test_repeated_approved_analysis_preserves_first_causal_availability():
    first = result(sentiment="POSITIVE", entry_eligible=True, error="", requires_verification=False,
                   confidence=0.95, reason="Binding contract confirmed",
                   events=(ExtractedEvent("MAJOR_CONTRACT", "CONFIRMED", "POSITIVE", "POSITIVE", "HIGH",
                                          False, "executed a binding contract"),))
    engine = EventEngine(Settings(news_policy="primary"))
    engine.register_analysis(first, now=NOW)
    later = replace(first, event=replace(first.event, received_at=NOW + timedelta(minutes=5)),
                    analyzed_at=NOW + timedelta(minutes=5))
    engine.register_analysis(later, now=later.analyzed_at)
    stored = next(iter(engine.analyses.values()))
    assert stored["analyzed_at"] == NOW.isoformat()
    approved = positive_event(list(engine.news.values()), "TEST", NOW + timedelta(seconds=1),
                              engine.settings, engine.analyses)
    assert approved is not None and approved.received_at == NOW


def test_register_only_pipeline_prunes_inactive_context_but_keeps_archive_and_known_risk(tmp_path):
    repo = Repository(tmp_path / "context.db")
    try:
        engine = EventEngine(Settings(news_policy="primary"), repository=repo)
        old_time = NOW - timedelta(days=3)
        old = result(event("old", published=old_time - timedelta(seconds=1), received=old_time))
        engine.register_analysis(old, now=old_time)
        hazard = event("hazard", published=NOW - timedelta(hours=23),
                       body="The issuer announces a registered direct offering.")
        engine.register_analysis(result(hazard, sentiment="NEGATIVE", dilution_risk=True, reason="Potential dilution"), now=NOW)
        engine.register_analysis(result(event("new")), now=NOW)
        assert {item.id for item in engine.news.values()} == {"rss:hazard", "rss:new"}
        assert {value["news_id"] for value in engine.analyses.values()} == {"rss:hazard", "rss:new"}
        assert len(repo.all("news_analysis")) == 3  # Historical journal is retained.
        assert len(repo.get_control("engine_context")["news"]) == 2
        assert positive_event(list(engine.news.values()), "TEST", NOW, engine.settings, engine.analyses) is None
    finally:
        repo.close()


def test_custom_hazard_window_is_not_silently_reduced_to_48_hours():
    engine = EventEngine(Settings(max_news_age_hours=96, news_policy="primary"))
    older = NOW - timedelta(hours=80)
    hazard = event("long-risk", published=older, received=older + timedelta(seconds=1),
                   body="The issuer announces a registered direct offering.")
    engine.register_analysis(result(hazard, sentiment="NEGATIVE", dilution_risk=True), now=NOW)
    engine.register_analysis(result(event("new")), now=NOW)
    assert {item.id for item in engine.news.values()} == {"rss:long-risk", "rss:new"}


def test_market_only_persistence_prunes_expired_context_without_a_new_news_event(tmp_path):
    repo = Repository(tmp_path / "idle-context.db")
    try:
        engine = EventEngine(Settings(), repository=repo)
        engine.register_analysis(result(), now=NOW)
        engine.last_time = NOW + timedelta(days=3)
        engine.persist()
        assert engine.news == {} and engine.analyses == {}
        assert len(repo.all("news_analysis")) == 1
        assert repo.get_control("engine_context")["news"] == []
    finally:
        repo.close()


def test_historical_replay_pruning_uses_replay_clock_not_current_wall_clock():
    historical = NOW - timedelta(days=1000)
    engine = EventEngine(Settings(), live=False)
    item = event("historical", published=historical - timedelta(seconds=1), received=historical)
    engine.register_analysis(result(item), now=historical)
    engine.last_time = historical
    engine.persist()
    assert len(engine.news) == len(engine.analyses) == 1
