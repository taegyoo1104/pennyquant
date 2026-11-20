"""Slow synchronous work must not authorize a buy using an expired book."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.universe import UniverseStore
from sessionquant.portfolio.models import OrderIntent, PortfolioSettings
from sessionquant.research import journal
from sessionquant.research.bundle import export_recording, file_hash
from sessionquant.research.replay import replay_recording
from sessionquant.session_demo import synthetic_tape
from sessionquant.session_engine import SessionEngine, event_time
from test_realtime_paper import observed  # noqa: F401


@pytest.fixture
def runtime(tmp_path):
    rows, opened = synthetic_tape()
    entry = opened+timedelta(minutes=75, seconds=1)
    clock = SimpleNamespace(now=entry)
    universe = UniverseStore()
    universe.refresh(['AAPL'], source='SYNTHETIC_FIXTURE', observed_at=opened-timedelta(days=1))
    repo = Repository(tmp_path/'runtime.db', backend='sqlite')
    options = dict(strategy='pullback_simple', synthetic=True, use_market_filter=False, use_news=False,
                   candidate_settings={'enabled':True}, record_inputs=True, execution_clock=lambda:clock.now)
    engine = SessionEngine(repo, universe, **options)
    initial = [row for row in rows if event_time(row)<=entry]
    quote = next(row for row in rows if row['kind']=='quote' and row['data']['symbol']=='AAPL')
    yield SimpleNamespace(repo=repo, universe=universe, engine=engine, options=options,
                          entry=entry, clock=clock, initial=initial, quote=quote)
    repo.close()


def book(value, offset, *, bid=None):
    row = deepcopy(value.quote)
    stamp = value.entry+timedelta(seconds=offset)
    row['data'].update(timestamp=stamp.isoformat(), received_at=stamp.isoformat())
    if bid is not None:
        row['data'].update(bid=bid, ask=bid+.02)
    return row


def apply(value, offset, *, bid=None, wall_offset=None):
    value.clock.now = value.entry+timedelta(seconds=offset if wall_offset is None else wall_offset)
    value.engine.apply_batch([book(value, offset, bid=bid)], value.entry+timedelta(seconds=offset))


@pytest.mark.parametrize('stage', ['ingest', 'ranking', 'risk_audit'])
def test_slow_submission_stage_rejects_without_consuming_signal_and_fresh_retry_works(runtime, monkeypatch, stage):
    value = runtime
    fired = False
    def delay():
        nonlocal fired
        if not fired:
            fired = True
            value.clock.now += timedelta(seconds=11)
    if stage == 'ingest':
        original = value.engine._ingest
        def slow(raw):
            original(raw)
            if raw['kind']=='quote':delay()
        monkeypatch.setattr(value.engine, '_ingest', slow)
    elif stage == 'ranking':
        original = value.engine.candidate_selector.rank
        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            delay()
            return result
        monkeypatch.setattr(value.engine.candidate_selector, 'rank', slow)
    else:
        original = value.repo.record
        def slow(kind, data):
            result = original(kind, data)
            if kind=='pipeline_status' and data.get('kind')=='risk_decision':delay()
            return result
        monkeypatch.setattr(value.repo, 'record', slow)
    value.engine.apply_batch(value.initial, value.entry)
    assert fired and not value.engine.broker.orders and not value.engine.consumed_signals
    assert value.repo.all('order_intents') == []
    assert value.engine.execution_guard.checks[-1]['reason'] == 'runtime_quote_stale_or_invalid'
    row = value.engine.selection_frame['rows'][0]
    assert not row['approved'] and row['order_intent_id'] is None
    assert row['allocation_reason'] == 'runtime_quote_stale_or_invalid'
    apply(value, 12)
    order = value.engine.broker.get_open_orders()[0]
    assert order.intent.created_at == value.entry+timedelta(seconds=12)
    assert len(value.engine.consumed_signals) == 1
    apply(value, 13)
    assert [fill.side for fill in value.engine.broker.fills] == ['buy']


@pytest.mark.parametrize('restart', [False, True])
def test_slow_ingestion_blocks_pending_fill_then_new_book_fills_after_restart(runtime, monkeypatch, restart):
    value = runtime
    value.engine.apply_batch(value.initial, value.entry)
    original = value.engine._ingest
    def slow(raw):
        original(raw)
        value.clock.now += timedelta(seconds=11)
    with monkeypatch.context() as patch:
        patch.setattr(value.engine, '_ingest', slow)
        apply(value, 1)
    assert not value.engine.broker.fills
    assert value.engine.broker.get_open_orders()[0].unfilled_reason == 'runtime_quote_stale_or_invalid'
    assert value.repo.load_state()['last_execution_check_at'] == value.clock.now.isoformat()
    if restart:
        value.engine = SessionEngine(value.repo, value.universe, restore=True, **value.options)
    apply(value, 13)
    assert [fill.side for fill in value.engine.broker.fills] == ['buy']


def test_runtime_clock_rollback_watermark_survives_restart(runtime):
    value = runtime
    value.clock.now += timedelta(seconds=11)
    value.engine.apply_batch(value.initial, value.entry)
    assert not value.engine.broker.orders
    value.engine = SessionEngine(value.repo, value.universe, restore=True, **value.options)
    apply(value, 2, wall_offset=10)
    assert not value.engine.broker.orders
    assert value.engine.execution_guard.checks[-1]['reason'] == 'runtime_clock_rollback'
    assert value.repo.load_state()['last_execution_check_at'] == (value.entry+timedelta(seconds=11)).isoformat()
    assert value.engine.last_decision_at == value.entry+timedelta(seconds=2)
    apply(value, 3, wall_offset=12)
    assert len(value.engine.broker.orders) == 1


def test_actual_entry_window_closing_blocks_pending_buy_even_with_fresh_quote(runtime):
    value = runtime
    ending = value.engine.exit_manager.schedule(value.entry).entry_end
    created = ending-timedelta(seconds=3)
    value.engine.broker.submit_order(OrderIntent('late-buy','AAPL','buy',created,'102',amount='20',stop_price='99'))
    row = deepcopy(value.quote)
    decision = ending-timedelta(seconds=1)
    row['data'].update(timestamp=decision.isoformat(), received_at=decision.isoformat())
    value.clock.now = ending+timedelta(seconds=1)
    value.engine.apply_batch([row], decision)
    assert not value.engine.broker.fills
    assert value.engine.broker.get_open_orders()[0].unfilled_reason == 'runtime_entry_window_closed'


def test_runtime_guard_does_not_change_sell_fills_or_stop_marks(runtime):
    value = runtime
    value.engine.apply_batch(value.initial, value.entry)
    apply(value, 1)
    apply(value, 2, bid=100, wall_offset=13)
    assert value.engine.broker.positions['AAPL'].mark_price == 100
    assert value.engine.broker.get_open_orders()[0].intent.reason == 'stop_loss'
    apply(value, 3, bid=100, wall_offset=14)
    assert [fill.side for fill in value.engine.broker.fills] == ['buy','sell']
    assert not value.engine.broker.positions and value.engine.execution_guard.checks == []


def test_order_audit_failure_after_submission_rolls_back_ledger_and_requires_restart(runtime, monkeypatch):
    value = runtime
    value.engine.tick(value.entry-timedelta(seconds=1))
    original = value.repo.record
    def fail(kind, data):
        if kind=='order_intents':raise RuntimeError('audit persistence failure')
        return original(kind, data)
    with monkeypatch.context() as patch:
        patch.setattr(value.repo, 'record', fail)
        with pytest.raises(RuntimeError, match='audit persistence'):
            value.engine.apply_batch(value.initial, value.entry)
    assert value.engine.failed and value.engine.broker.orders
    assert value.repo.all('order_intents') == []
    recovered = SessionEngine(value.repo, value.universe, restore=True, **value.options)
    assert not recovered.broker.orders and not recovered.consumed_signals


def policy_runtime(request, policy, *, expires_after):
    if policy == 'strict':
        value = request.getfixturevalue('runtime')
    else:
        source = request.getfixturevalue('observed')
        rows, opened = synthetic_tape()
        entry = opened+timedelta(minutes=75, seconds=1)
        clock = SimpleNamespace(now=entry)
        source.engine.strategy = source.engine.market.strategy = 'pullback_simple'
        source.engine.use_market_filter = source.engine.market.use_market_filter = False
        source.engine.use_news = False
        source.engine.execution_guard.clock = lambda:clock.now
        value = SimpleNamespace(repo=source.repo, engine=source.engine, universe=source.universe,
            entry=entry, clock=clock, initial=[row for row in source.rows if event_time(row)<=entry],
            quote=next(row for row in source.rows if row['kind']=='quote' and row['data']['symbol']=='AAPL'))
    value.initial = sorted(deepcopy(value.initial), key=event_time)
    expiry = (value.entry+timedelta(seconds=expires_after)).isoformat()
    item = next(row['data'] for row in value.initial if row['kind']=='eligibility')
    item['valid_until'] = expiry
    if policy != 'strict':item['field_evidence']['listing_status']['valid_until'] = expiry
    return value


@pytest.mark.parametrize('policy', ['strict', 'observed_paper_v1'])
def test_eligibility_expiring_during_submission_work_blocks_unconsumed_signal(request, policy):
    value = policy_runtime(request, policy, expires_after=2)
    value.clock.now = value.entry+timedelta(seconds=3)
    value.engine.apply_batch(value.initial, value.entry)
    assert not value.engine.broker.orders and not value.engine.consumed_signals
    assert value.engine.execution_guard.checks[-1]['reason'] == 'runtime_instrument_eligibility_restricted'
    assert value.engine.selection_frame['rows'][0]['allocation_reason'] == 'runtime_instrument_eligibility_restricted'


@pytest.mark.parametrize('policy', ['strict', 'observed_paper_v1'])
def test_eligibility_expiring_before_actual_fill_blocks_and_replays_exactly(request, policy, tmp_path, monkeypatch):
    value = policy_runtime(request, policy, expires_after=2)
    monkeypatch.setattr(journal, 'clock', lambda:value.clock.now)
    journal.start(value.repo, value.engine, source='SYNTHETIC_RUNTIME')
    value.repo.record('universe_history', value.universe.snapshots[0])
    value.engine.apply_batch(value.initial, value.entry)
    assert len(value.engine.broker.get_open_orders()) == 1
    apply(value, 1, wall_offset=3)
    assert not value.engine.broker.fills
    assert value.engine.broker.get_open_orders()[0].unfilled_reason == 'runtime_instrument_eligibility_restricted'
    journal.stop(value.repo)
    bundle = tmp_path/'expiry-recording'
    export_recording(value.repo, bundle)
    result = replay_recording(bundle, tmp_path/'expiry-replay.db', use_market_filter=False, use_news=False)
    assert result['state'] == value.engine.broker.to_state()
    assert result['execution_guard_denials'] == {'runtime_instrument_eligibility_restricted':1}


@pytest.mark.parametrize('policy', ['strict', 'observed_paper_v1'])
def test_fresh_eligibility_allows_fill_for_both_policies(request, policy):
    value = policy_runtime(request, policy, expires_after=5)
    value.engine.apply_batch(value.initial, value.entry)
    apply(value, 1)
    assert [fill.side for fill in value.engine.broker.fills] == ['buy']
    if policy != 'strict':assert value.engine.market.quotes['AAPL'].tradable is False


@pytest.mark.parametrize('field, restricted', [('tradable',False), ('fractional',False), ('halted',True), ('corporate_action',True)])
def test_runtime_pending_strict_buy_respects_new_instrument_restriction(runtime, field, restricted):
    value = runtime
    value.engine.apply_batch(value.initial, value.entry)
    changed = deepcopy(next(row for row in value.initial if row['kind']=='eligibility'))
    stamp = value.entry+timedelta(seconds=1)
    changed['data'].update(checked_at=stamp.isoformat(), **{field:restricted})
    value.clock.now = stamp
    value.engine.apply_batch([changed, book(value, 1)], stamp)
    assert not value.engine.broker.fills
    assert value.engine.broker.get_open_orders()[0].unfilled_reason == 'runtime_instrument_eligibility_restricted'


@pytest.mark.parametrize('model, deadline', [('market_v2','ttl'), ('legacy_limit_v1','ttl'), ('market_v2','cancel')])
def test_actual_order_deadline_blocks_fresh_book_and_replays_exactly(runtime, tmp_path, monkeypatch, model, deadline):
    value = runtime
    settings = PortfolioSettings(paper_fill_model=model, order_ttl_seconds=60 if deadline=='ttl' else 120,
                                 paper_cancel_latency_seconds='5')
    value.engine = SessionEngine(value.repo, value.universe, settings=settings, **value.options)
    monkeypatch.setattr(journal, 'clock', lambda:value.clock.now)
    journal.start(value.repo, value.engine, source='SYNTHETIC_RUNTIME')
    value.repo.record('universe_history', value.universe.snapshots[0])
    value.engine.apply_batch(value.initial, value.entry)
    if deadline == 'cancel':
        value.clock.now = value.entry+timedelta(seconds=56)
        value.repo.set_control('manual_halt', True)
        value.engine.tick(value.clock.now)
        assert value.engine.broker.get_open_orders()[0].cancel_effective_at == value.entry+timedelta(seconds=61)
    changed = deepcopy(next(row for row in value.initial if row['kind']=='eligibility'))
    logical = value.entry+timedelta(seconds=59)
    changed['data']['checked_at'] = logical.isoformat()
    value.clock.now = value.entry+timedelta(seconds=61)
    value.engine.apply_batch([changed, book(value, 59)], logical)
    assert not value.engine.broker.fills
    assert value.engine.broker.get_open_orders()[0].unfilled_reason == 'runtime_order_no_longer_active'
    journal.stop(value.repo)
    bundle = tmp_path/'deadline-recording'
    export_recording(value.repo, bundle)
    result = replay_recording(bundle, tmp_path/'deadline-replay.db', use_market_filter=False, use_news=False)
    assert result['state'] == value.engine.broker.to_state()
    assert result['execution_guard_denials'] == {'runtime_order_no_longer_active':1}


def record(value, tmp_path, monkeypatch, *, delayed=False):
    monkeypatch.setattr(journal, 'clock', lambda:value.clock.now)
    journal.start(value.repo, value.engine, source='SYNTHETIC_RUNTIME')
    value.repo.record('universe_history', value.universe.snapshots[0])
    if delayed:value.clock.now += timedelta(seconds=11)
    value.engine.apply_batch(value.initial, value.entry)
    if delayed:
        apply(value, 12)
        apply(value, 13, wall_offset=24)
        apply(value, 25)
    else:
        apply(value, 1)
    journal.stop(value.repo)
    bundle = tmp_path/'recording'
    export_recording(value.repo, bundle)
    return bundle


def test_guarded_recording_preserves_exact_results_and_restart(runtime, tmp_path, monkeypatch):
    bundle = record(runtime, tmp_path, monkeypatch, delayed=True)
    result = replay_recording(bundle, tmp_path/'replay.db', use_market_filter=False, use_news=False)
    assert result['state'] == runtime.engine.broker.to_state()
    assert result['execution_guard_denials'] == {'runtime_quote_stale_or_invalid':2}
    resumed = replay_recording(bundle, tmp_path/'replay.db', use_market_filter=False, use_news=False, resume=True)
    assert resumed['state'] == result['state'] and resumed['execution_guard_denials'] == result['execution_guard_denials']
    frames = [json.loads(line) for line in (bundle/'events.jsonl').read_text().splitlines()
              if json.loads(line)['kind']=='engine_batch']
    assert frames[0]['data']['execution_checks'][0]['order_id'].startswith('sq-buy-')
    assert frames[0]['data']['decision_at'] == runtime.entry.isoformat()
    assert frames[0]['data']['execution_checks'][0]['checked_at'] == (runtime.entry+timedelta(seconds=11)).isoformat()


def rewrite_checks(bundle, change, *, old_format=False):
    rows = [json.loads(line) for line in (bundle/'events.jsonl').read_text().splitlines()]
    for row in rows:
        if row['kind']=='engine_batch':
            change(row['data'])
        if old_format and row['kind']=='recording_start':
            row['data'].pop('execution_guard_version', None)
        if row['kind']=='engine_batch' or (old_format and row['kind']=='recording_start'):
            row['id'] = 'research:'+journal.identity({key:value for key,value in row.items()
                if key not in {'id','ingested_at','source_sequence'}})
    (bundle/'events.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    manifest_path = bundle/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if old_format:
        manifest['recording_starts'] = [row for row in rows if row['kind']=='recording_start']
    manifest['files']['events.jsonl'] = file_hash(bundle/'events.jsonl')
    manifest['bundle_id'] = journal.identity({'files':manifest['files'], 'starts':manifest['recording_starts'],
                                            'synthetic':manifest['synthetic']})
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize('missing', ['absent', 'null', 'empty', 'mismatch'])
def test_new_guard_recording_missing_or_mismatched_evidence_blocks_and_reports(runtime, tmp_path, monkeypatch, missing):
    bundle = record(runtime, tmp_path, monkeypatch)
    def change(data):
        if missing=='absent':
            data.pop('execution_checks', None)
            data.pop('execution_guard_version', None)
        elif missing=='null':data['execution_checks']=None
        elif missing=='empty':data['execution_checks']=[]
        else:
            for check in data['execution_checks']:check['order_id']='another-order'
    rewrite_checks(bundle, change)
    result = replay_recording(bundle, tmp_path/'replay.db', use_market_filter=False, use_news=False)
    assert not result['state']['orders'] and not result['state']['fills']
    reason = 'runtime_execution_check_mismatch' if missing=='mismatch' else (
        'runtime_execution_check_missing' if missing=='empty' else 'runtime_execution_check_invalid')
    assert result['execution_guard_denials'][reason] > 0
    assert any(row['reason']==reason for row in result['risk_decisions'])


def test_counterfactual_order_cannot_borrow_recorded_fill_permission(runtime, tmp_path, monkeypatch):
    bundle = record(runtime, tmp_path, monkeypatch)
    result = replay_recording(bundle, tmp_path/'changed-window.db', use_market_filter=False, use_news=False,
                              entry_start=(runtime.entry+timedelta(seconds=1)).isoformat())
    assert not result['state']['orders'] and not result['state']['fills']
    assert result['execution_guard_denials']['runtime_execution_check_mismatch'] > 0


def test_old_recording_without_execution_guard_fields_preserves_logical_results(runtime, tmp_path, monkeypatch):
    runtime.engine.execution_guard.clock = None
    bundle = record(runtime, tmp_path, monkeypatch)
    def old(data):
        for name in ('execution_checks', 'execution_guard_version', 'execution_check_overflow'):
            data.pop(name, None)
    rewrite_checks(bundle, old, old_format=True)
    result = replay_recording(bundle, tmp_path/'old-replay.db', use_market_filter=False, use_news=False)
    assert result['state'] == runtime.engine.broker.to_state()
    assert result['execution_guard_denials'] == {}


@pytest.mark.parametrize('transport', ['toss', 'file'])
async def test_only_market_runtime_injects_actual_clock(tmp_path, monkeypatch, transport):
    from sessionquant.session_config import SessionConfig
    from sessionquant.session_runtime import run_session
    from sessionquant import session_runtime
    now = datetime.now(timezone.utc)
    universe_path = tmp_path/'universe.json'
    UniverseStore(universe_path).refresh(['AAPL'], source='SYNTHETIC_FIXTURE', observed_at=now)
    source = tmp_path/'feed.jsonl'
    source.touch()
    engines = []
    def capture(*args, **kwargs):
        engine = SessionEngine(*args, **kwargs)
        engines.append(engine)
        return engine
    monkeypatch.setattr(session_runtime, 'SessionEngine', capture)
    from sessionquant.market.source import MarketBatch
    class Market:
        async def poll(self, *args, **kwargs):return MarketBatch()
        async def aclose(self):pass
    monkeypatch.setattr(session_runtime, 'TossSessionSource', lambda **kwargs:Market())
    config = SessionConfig(database_backend='sqlite', db_path=tmp_path/'run.db', universe_path=universe_path,
        source=transport, input_path=source if transport=='file' else None, news_enabled=False, watchlist_enabled=False,
        actual_account_monitor_enabled=False, market_filter_enabled=False)
    await run_session(config, no_dashboard=True, max_cycles=1, desktop_notifications=False)
    assert engines[0].execution_guard.enabled is (transport=='toss')
    if transport=='toss':assert engines[0].execution_guard.clock() >= now
    repo = Repository(tmp_path/'direct.db', backend='sqlite')
    try:
        assert not SessionEngine(repo, UniverseStore()).execution_guard.enabled
    finally:
        repo.close()
