from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from sessionquant.config.settings import Settings
from sessionquant.models import NewsEvent
from sessionquant.news.analysis import NewsAnalyzer, event_digest
from sessionquant.news.event_filter import eligible_events, positive_event


NOW = datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)
BODY = "The Phase 3 study met the primary endpoint with a statistically significant result."


def article(**updates):
    return replace(NewsEvent("rss:one", ("TEST",), "Company reports clinical results",
                             NOW - timedelta(minutes=1), NOW - timedelta(seconds=2),
                             "Company IR RSS", "https://issuer.test/release", BODY, True, False), **updates)


def extraction(**updates):
    return {"ticker": "TEST", "events": [{"type": "CLINICAL_SUCCESS", "status": "CONFIRMED",
            "company_impact": "POSITIVE", "shareholder_impact": "POSITIVE", "importance": "HIGH",
            "dilution_risk": False, "evidence": "study met the primary endpoint"}],
            "overall_sentiment": "POSITIVE", "confidence": 0.9, "requires_verification": False,
            "missing_information": [], "reason": "Primary endpoint achieved", **updates}


def response(data=None, **updates):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(data or extraction())}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 70}, **updates}


async def test_llm_missing_key_never_silently_authorizes_rule_positive():
    analyzer = NewsAnalyzer(api_key="")
    try:
        result = await analyzer.analyze(article(), "TEST", source_kind="rss", primary_verified=True, now=NOW)
    finally:
        await analyzer.aclose()
    assert result.sentiment == "UNKNOWN" and result.error == "llm_key_missing"
    assert not result.entry_eligible and analyzer.calls == 0


async def test_verified_primary_evidence_is_structured_and_completion_is_available_time(monkeypatch):
    times = iter([100.0, 102.5])
    monkeypatch.setattr("sessionquant.news.analysis.monotonic", lambda: next(times))
    def handle(request):
        payload = json.loads(request.content)
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert "untrusted article DATA" in payload["messages"][0]["content"]
        assert request.headers["Authorization"] == "Bearer SECRET"
        return httpx.Response(200, json=response())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        analyzer = NewsAnalyzer(api_key="SECRET", client=client)
        result = await analyzer.analyze(article(), "TEST", source_kind="rss", primary_verified=True, now=NOW)
    assert result.sentiment == "POSITIVE" and result.entry_eligible and result.primary_verified
    assert result.analyzed_at == NOW + timedelta(seconds=2.5) and result.latency_ms == 2500
    assert result.input_tokens == 100 and result.output_tokens == 70
    assert result.input_digest == event_digest(article(), "TEST")
    json.dumps(result.to_dict())


@pytest.mark.parametrize("kind,primary,event", [
    ("alpaca", True, article(id="alpaca:one", source="wire")),
    ("rss", False, article()),
    ("sec", True, article(id="sec:one", url="https://not-sec.test/release")),
])
async def test_auxiliary_or_unconfirmed_primary_cannot_authorize_entry(kind, primary, event):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response()))) as client:
        result = await NewsAnalyzer(api_key="key", client=client).analyze(
            event, "TEST", source_kind=kind, primary_verified=primary, now=NOW)
    if kind in {'sec','rss'}:
        assert result.sentiment=='UNKNOWN' and result.error=='primary_source_unverified'
    else:assert result.sentiment == "POSITIVE"
    assert not result.entry_eligible


@pytest.mark.parametrize("change", [
    {"ticker": "OTHER"}, {"confidence": 1.1}, {"confidence": True}, {"unexpected": "value"},
    {"events": [{**extraction()["events"][0], "evidence": "An invented quotation"}]},
    {"events": [{**extraction()["events"][0], "status": "ALMOST_CONFIRMED"}]},
])
async def test_invalid_model_schema_or_evidence_is_unknown(change):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response(extraction(**change))),
    )) as client:
        result = await NewsAnalyzer(api_key="key", client=client).analyze(
            article(), "TEST", source_kind="rss", primary_verified=True, now=NOW)
    assert result.sentiment == "UNKNOWN" and result.error == "llm_invalid_output" and not result.entry_eligible


@pytest.mark.parametrize("change", [
    {"requires_verification": True}, {"confidence": 0.5}, {"missing_information": ["issuer confirmation"]},
    {"events": [{**extraction()["events"][0], "status": "PLANNED"}]},
])
async def test_uncertain_or_planned_event_cannot_authorize_entry(change):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response(extraction(**change))),
    )) as client:
        result = await NewsAnalyzer(api_key="key", client=client).analyze(
            article(), "TEST", source_kind="rss", primary_verified=True, now=NOW)
    assert not result.entry_eligible


async def test_known_dilution_overrides_positive_model_and_needs_no_paid_call():
    analyzer = NewsAnalyzer(api_key="")
    try:
        result = await analyzer.analyze(article(title="Company announces public offering"), "TEST",
                                        source_kind="rss", primary_verified=True, now=NOW)
    finally:
        await analyzer.aclose()
    assert result.sentiment == "NEGATIVE" and result.dilution_risk and not result.entry_eligible
    assert result.model == "rules-v2" and analyzer.calls == 0


async def test_llm_output_dilution_cannot_be_overruled_by_overall_positive():
    data = extraction(events=[{**extraction()["events"][0], "type": "ATM_OFFERING", "dilution_risk": True}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(data)))) as client:
        result = await NewsAnalyzer(api_key="key", client=client).analyze(
            article(), "TEST", source_kind="rss", primary_verified=True, now=NOW)
    assert result.sentiment == "NEGATIVE" and result.dilution_risk and not result.entry_eligible


async def test_unchanged_event_uses_original_receipt_and_approval_but_updates_reanalyze():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response()))) as client:
        analyzer = NewsAnalyzer(api_key="key", client=client)
        first = await analyzer.analyze(article(), "TEST", source_kind="rss", primary_verified=True, now=NOW)
        later = await analyzer.analyze(article(received_at=NOW + timedelta(seconds=10)), "TEST",
                                       source_kind="rss", primary_verified=True, now=NOW + timedelta(seconds=10))
        updated = await analyzer.analyze(article(body=BODY + " More detail."), "TEST",
                                         source_kind="rss", primary_verified=True, now=NOW + timedelta(seconds=20))
    assert analyzer.calls == 2
    assert first.analyzed_at == later.analyzed_at and first.event.received_at == later.event.received_at
    assert updated.input_digest != first.input_digest


@pytest.mark.parametrize("code", [401, 429, 500])
async def test_llm_errors_are_fixed_unknown_without_secret_body(code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(code, text="PRIVATE-response SECRET-key"),
    )) as client:
        result = await NewsAnalyzer(api_key="SECRET", client=client).analyze(
            article(), "TEST", source_kind="rss", primary_verified=True, now=NOW)
    assert result.error == f"llm_http_{code}" and not result.entry_eligible
    assert "SECRET" not in json.dumps(result.to_dict()) and "PRIVATE" not in result.reason


async def test_primary_policy_uses_completed_evidence_without_realtime_or_120second_requirement():
    event = article(published_at=NOW - timedelta(minutes=20))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response()))) as client:
        analysis = await NewsAnalyzer(api_key="key", client=client).analyze(
            event, "TEST", source_kind="rss", primary_verified=True, now=NOW)
    registry = {f"{event.id}:TEST": analysis.to_dict()}
    settings = Settings(news_policy="primary")
    assert positive_event([event], "TEST", NOW, settings, registry) is None
    approved = positive_event([event], "TEST", NOW + timedelta(seconds=1), settings, registry)
    assert approved is not None and approved.received_at == analysis.analyzed_at and not approved.real_time
    assert positive_event([replace(event, body="Updated different evidence")], "TEST",
                          NOW + timedelta(seconds=1), settings, registry) is None


async def test_primary_policy_rejects_alpaca_stale_or_future_and_keeps_negative_hazards():
    event = article()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response()))) as client:
        analysis = await NewsAnalyzer(api_key="key", client=client).analyze(
            event, "TEST", source_kind="rss", primary_verified=True, now=NOW)
    registry = {f"{event.id}:TEST": analysis.to_dict()}
    settings = Settings(news_policy="primary")
    later = NOW + timedelta(seconds=1)
    assert positive_event([event], "TEST", later, settings, registry) is not None
    assert positive_event([event], "TEST", NOW + timedelta(hours=5), settings, registry) is None
    bad_registry = {f"{event.id}:TEST": {**analysis.to_dict(), "source_kind": "alpaca"}}
    assert eligible_events([event], "TEST", later, settings, bad_registry) == []
    hazard = article(id="alpaca:risk", title="Company announces public offering")
    assert positive_event([event, hazard], "TEST", later, settings, registry) is None


async def test_llm_call_limit_does_not_send_request():
    def handle(request):
        pytest.fail("No paid call after the configured call limit")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await NewsAnalyzer(api_key="key", client=client, max_calls=0).analyze(
            article(), "TEST", source_kind="rss", primary_verified=True, now=NOW)
    assert result.error == "llm_call_limit" and not result.entry_eligible


@pytest.mark.parametrize('evidence,valid',[
    ('Company reports clinical results',False),
    ('임상시험에서 목표를 달성했다',False),
    ('study met the primary endpoint',True),
])
async def test_body_evidence_remains_required_after_prompt_clarification(evidence,valid):
    def handle(request):
        prompt=json.loads(request.content)['messages'][0]['content']
        assert 'single body paragraph' in prompt and 'not evidence' in prompt
        value=extraction()
        value['events'][0]['evidence']=evidence
        return httpx.Response(200,json=response(value))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await NewsAnalyzer(api_key='fixture',client=client).analyze(
            article(),'TEST',source_kind='rss',primary_verified=True,now=NOW)
    assert result.entry_eligible is valid
    if valid:assert result.events[0].evidence==evidence
    else:assert result.validation_error_code=='EVIDENCE_QUOTE_MISMATCH'
