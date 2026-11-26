from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from sessionquant.macro.service import NewsPipeline
from sessionquant.research.news_diagnostics import summarize
from sessionquant.research.storage import StoragePolicy
from sessionquant.session_engine import SessionEngine

NOW = datetime(2026, 11, 26, 16, tzinfo=timezone.utc)


def fixture_news(count=10000):
    context = {'status': 'AVAILABLE', 'block_entries': True, 'major_adverse': True,
               'blackout': False, 'reasons': ['news reason' * 1000] * 200,
               'candidate_score_signal': {'status': 'VERIFIED', 'value': 0,
                   'analysis_ids': [f'a{i}' for i in range(count)], 'llm_call_ids': ['call-1']}}
    evidence = {'analyses': [{'id': f'a{i}', 'model': 'test', 'prompt_version': 'v1',
        'published_at': '2026-11-26T13:00:00Z', 'received_at': '2026-11-26T13:01:00Z',
        'analyzed_at': '2026-11-26T14:00:00Z'} for i in range(count)], 'economic_events': []}
    return context, evidence


def test_large_news_diagnostic_is_bounded_without_changing_decision_or_evidence():
    context, evidence = fixture_news()
    original = deepcopy((context, evidence))
    result = summarize(context, evidence)
    assert (context, evidence) == original
    assert len(json.dumps(result).encode()) < 12000
    assert len(json.dumps(original).encode()) > 2_000_000
    assert result['full_evidence'] is False
    assert result['context']['block_entries'] is True
    assert result['analyses']['count'] == 10000
    assert result['analyses']['omitted'] == 9984
    assert result['analyses']['sample'][0]['id'] == 'a0'
    assert result['analyses']['sample'][0]['analyzed_at'] == '2026-11-26T14:00:00Z'
    assert result['context']['candidate_score_signal']['analysis_ids']['omitted'] == 9984


def test_engine_uses_diagnostic_provider_only_for_bounded_recording():
    context, evidence = fixture_news(20)
    calls = []
    class Provider:
        def recorded_evidence(self, now, symbol):
            calls.append('full')
            return evidence
        def diagnostic_evidence(self, now, symbol):
            calls.append('diagnostic')
            return evidence
    for mode in ('full', 'bounded_diagnostics'):
        engine = SimpleNamespace(_risk_context=lambda now, symbol: context, strategy='baseline',
            record_inputs=True, risk_provider=Provider(), auxiliary_storage=StoragePolicy(mode=mode),
            _batch_news_contexts={})
        assert SessionEngine.risk_context(engine, NOW, 'AAPL') is context
        captured = engine._batch_news_contexts['AAPL']
        if mode == 'full':
            assert captured == {'context': context, 'evidence': evidence, 'available_at': NOW.isoformat()}
        else:
            assert captured['format'] == 'news_diagnostic_v1'
            assert 'evidence' not in captured
    assert calls == ['full', 'diagnostic']


def test_news_pipeline_counts_matching_versions_but_only_serializes_sample():
    rows = {f'a{i}': {'id': f'a{i}', 'category': 'macro', 'model': 'm', 'prompt_version': 'v',
        'published_at': '2026-11-26T13:00:00Z', 'received_at': '2026-11-26T13:01:00Z',
        'analyzed_at': '2026-11-26T14:00:00Z'} for i in range(100)}
    rows['other'] = {**rows['a0'], 'category': 'company', 'ticker': 'OTHER'}
    pipeline = object.__new__(NewsPipeline)
    pipeline._analyses = rows
    pipeline.economic_events_at = lambda now: []
    full = pipeline.recorded_evidence(NOW, 'AAPL')
    diagnostic = pipeline.diagnostic_evidence(NOW, 'AAPL')
    assert len(full['analyses']) == 100 and 'analyses_count' not in full
    assert len(diagnostic['analyses']) == 16 and diagnostic['analyses_count'] == 100
    assert diagnostic['analyses'] == full['analyses'][:16]


def test_overlong_references_are_not_silently_rewritten_as_valid_ids():
    result = summarize({'candidate_score_signal': {'analysis_ids': ['x' * 300]}},
                       {'analyses': [{'id': 'x' * 300, 'model': {'unexpected': 'large'}}]})
    assert result['context']['candidate_score_signal']['analysis_ids'] == {
        'sample': [], 'count': 1, 'omitted': 1}
    assert result['analyses']['sample'][0]['id'] is None
    assert result['analyses']['sample'][0]['model'] is None


def test_compact_engine_frame_survives_size_gate_and_keeps_primary_news(tmp_path, monkeypatch):
    from sessionquant.database.repository import Repository
    from sessionquant.market.universe import UniverseStore
    from sessionquant.research import journal
    from sessionquant.session_demo import synthetic_tape
    from sessionquant.session_engine import event_time
    tape, opening = synthetic_tape()
    context, evidence = fixture_news()
    states = []
    for mode in ('full', 'bounded_diagnostics'):
        repo = Repository(tmp_path / (mode + '.db'), backend='sqlite')
        try:
            universe = UniverseStore()
            universe.refresh(['AAPL'], source='SYNTHETIC_FIXTURE', observed_at=opening-timedelta(days=1))
            engine = SessionEngine(repo, universe, synthetic=True, record_inputs=True,
                                   auxiliary_storage=asdict(StoragePolicy(mode=mode)))
            # Same neutral decision and huge evidence in both modes.
            engine.risk_provider = SimpleNamespace(risk_context=lambda *a, **k: {
                'status': 'AVAILABLE', 'block_entries': False, 'major_adverse': False,
                'blackout': False, 'reasons': []}, recorded_evidence=lambda *a: evidence)
            monkeypatch.setattr(journal, 'clock', lambda: opening)
            journal.start(repo, engine, source='fixture')
            repo.record('news_events', {'id': 'original-news', 'body': 'preserved'})
            for now in sorted({event_time(row) for row in tape}):
                monkeypatch.setattr(journal, 'clock', lambda: now)
                engine.apply_batch([row for row in tape if event_time(row) == now], now)
            frames = [r for r in repo.all('research_events') if r['kind'] == 'engine_batch'
                      and r['data']['news_contexts']]
            assert frames
            if mode == 'bounded_diagnostics':
                assert max(len(json.dumps(r).encode()) for r in frames) < 30000
            else:
                assert max(len(json.dumps(r).encode()) for r in frames) > 1_000_000
            states.append(engine.broker.to_state())
            assert len(repo.all('executions')) == 2
            assert any(row.get('body') == 'preserved' for row in repo.all('news_events'))
        finally:
            repo.close()
    assert states[0] == states[1]
