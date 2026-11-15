"""시장 참고 관측의 시각·권한·저장·예산을 합성 데이터로 검증한다."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market_reference.collector import MarketReferenceMonitor
from sessionquant.market_reference.contracts import REFERENCES, observation
from sessionquant.market_reference.store import ReferenceStore, OBSERVATIONS, METRICS

NOW = datetime(2026, 11, 13, 17, tzinfo=timezone.utc)


def proof(symbol='IEF', *, latency='REALTIME'):
    return {'verified': True, 'symbols': [symbol], 'scope': 'MARKET_REFERENCE',
        'source': 'SYNTHETIC_FIXTURE', 'evidence_url': 'https://fixture.invalid/entitlements',
        'checked_at': NOW.isoformat(), 'valid_until': (NOW+timedelta(days=1)).isoformat(),
        'latency_class': latency, 'realtime_entitled': latency == 'REALTIME',
        'price_comparability_verified': True, 'comparability_source': 'SYNTHETIC_ACTIONS_REVIEW',
        'range_start': (NOW-timedelta(days=3)).isoformat(), 'range_end': NOW.isoformat()}


def candle(symbol='IEF', minutes=0, price='100', **kwargs):
    at = NOW-timedelta(minutes=minutes)
    return observation(symbol, 'candle', {'open': price, 'high': price, 'low': price,
        'close': price, 'volume': '100', 'currency': 'USD'}, at, NOW, NOW,
        proof=kwargs.pop('proof', proof(symbol)), max_age=300, synthetic=True, **kwargs)


def series(store, symbol='IEF', final='100'):
    for minute in range(30, -1, -1):
        store.ingest(candle(symbol, minute, final if minute == 0 else '100'))
    return store.publish(NOW)['items'][symbol]


@pytest.fixture
def store(tmp_path):
    repo = Repository(tmp_path/'reference.db', backend='sqlite')
    yield ReferenceStore(repo)
    repo.close()


def test_complete_decimal_windows_and_etf_definitions(store):
    item = series(store, final='99.5')
    assert item['quality_status'] == 'GOOD'
    for minutes in (1, 5, 15, 30):
        row = item['windows'][str(minutes)]
        assert Decimal(row['pct_change']) == Decimal('-.5')
        assert row['sample_count'] == minutes+1 and row['quality_status'] == 'GOOD'
        assert len(row['input_ids']) == minutes+1
    assert item['reference_kind'] == 'INTERMEDIATE_TREASURY'
    assert REFERENCES['TLT'][0] == 'LONG_TREASURY'
    assert REFERENCES['USO'][0] == 'OIL_FUTURES_PROXY'
    assert not any('yield' in key or 'bps' in key or 'spot' in key for key in item)
    assert store.repo.all('orders') == []


@pytest.mark.parametrize('mode', ['missing_time', 'delayed', 'unverified', 'currency', 'future', 'stale'])
def test_no_receipt_time_or_http_success_promotes_quality(mode):
    stamp = NOW
    license = proof()
    values = {'price': '100', 'currency': 'USD'}
    if mode == 'missing_time': stamp = None
    if mode == 'delayed': stamp -= timedelta(minutes=15); license['latency_class'] = 'DELAYED'
    if mode == 'unverified': license = {}
    if mode == 'currency': values['currency'] = 'KRW'
    if mode == 'future': stamp += timedelta(seconds=1)
    if mode == 'stale': stamp -= timedelta(seconds=61)
    row = observation('IEF', 'price', values, stamp, NOW, NOW, proof=license)
    assert row['quality_status'] != 'GOOD'
    if mode == 'missing_time': assert row['event_at'] is None and row['valid_until'] is None
    if mode == 'delayed': assert row['latency_class'] == 'DELAYED' and row['freshness_seconds'] == 900
    assert row['received_at'] == NOW.isoformat()


@pytest.mark.parametrize('correction', ['split', 'dividend', 'adjustment_unknown', 'expired', 'missing_sample', 'zero_volume'])
def test_comparability_missing_samples_and_revisions_are_not_verified(store, correction):
    series(store)
    if correction in {'split', 'dividend'}:
        changed = candle(minutes=5, price='50' if correction == 'split' else '99')
        store.ingest(changed)
        assert store.state['latest']['IEF:candle']['event_at'] == NOW.isoformat()
        assert any(r['quality_status'] == 'REVISION_PENDING' for r in store.repo.all(OBSERVATIONS))
    elif correction == 'adjustment_unknown':
        for row in store.state['rows']['IEF:candle']: row['provider_permissions']['price_comparability_verified'] = False
    elif correction == 'expired':
        for row in store.state['rows']['IEF:candle']: row['provider_permissions']['valid_until'] = (NOW-timedelta(seconds=1)).isoformat()
    elif correction == 'missing_sample':
        store.state['rows']['IEF:candle'] = [r for r in store.state['rows']['IEF:candle'] if r['event_at'] != (NOW-timedelta(minutes=5)).isoformat()]
    else:
        zero = candle(minutes=5); zero['values']['volume'] = '0'
        store.state['rows']['IEF:candle'][25] = zero
    item = store.publish(NOW)['items']['IEF']
    assert item['windows']['15']['quality_status'] == 'UNKNOWN'


def test_duplicate_does_not_refresh_availability_and_restore_preserves_ids(store):
    original = candle()
    store.ingest(original)
    repeated = deepcopy(original)
    repeated['received_at'] = repeated['ingested_at'] = repeated['available_at'] = (NOW+timedelta(seconds=50)).isoformat()
    assert store.ingest(repeated)['available_at'] == NOW.isoformat()
    assert len(store.repo.all(OBSERVATIONS)) == 1 and store.state['duplicate_count'] == 1
    restored = ReferenceStore(store.repo)
    assert restored.rows('IEF', 'candle', NOW)[0]['id'] == original['id']
    assert restored.rows('IEF', 'candle', NOW-timedelta(seconds=1)) == []
    store.publish(NOW)
    count = len(store.repo.all(METRICS))
    store.publish(NOW)
    assert len(store.repo.all(METRICS)) == count


def test_failed_latest_request_invalidates_old_complete_series(store):
    assert series(store)['quality_status'] == 'GOOD'
    store.ingest(observation('IEF', 'candle', {}, None, NOW, NOW, error='http_429'))
    assert store.publish(NOW)['items']['IEF']['windows']['15']['quality_status'] == 'UNKNOWN'


@pytest.mark.parametrize('at', [datetime(2026, 11, 14, 17, tzinfo=timezone.utc),
    datetime(2026, 11, 26, 17, tzinfo=timezone.utc), datetime(2026, 11, 27, 18, 1, tzinfo=timezone.utc)])
async def test_weekend_holiday_and_early_close_do_not_fabricate_reads(store, at):
    async def forbidden(*args): pytest.fail('휴장·폐장 후 조회가 발생했습니다.')
    monitor = MarketReferenceMonitor(store.repo, clock=lambda: at)
    result = await monitor.poll(forbidden)
    assert result['items']['IEF']['quality_status'] == 'UNKNOWN'
    assert store.repo.all(OBSERVATIONS) == []


@pytest.mark.parametrize('at,opening', [('2026-10-30T15:00:00+00:00', '2026-10-30T13:30:00+00:00'),
    ('2026-11-02T16:00:00+00:00', '2026-11-02T14:30:00+00:00'),
    ('2026-11-27T17:00:00+00:00', '2026-11-27T14:30:00+00:00')])
def test_dst_and_early_close_calendar(at, opening):
    row = observation('SHY', 'price', {'price': '82', 'currency': 'USD'}, at, at, at)
    assert row['market_session']['opens_at'] == opening
    if '11-27' in at: assert row['market_session']['closes_at'] == '2026-11-27T18:00:00+00:00'


async def test_collector_rejects_duplicate_symbols_and_retains_error_only(store):
    monitor = MarketReferenceMonitor(store.repo, clock=lambda: NOW)
    async def read(stream, symbol):
        if stream == 'stocks': return {'data': []}
        return {'data': [{'symbol': 'IEF', 'lastPrice': '100', 'currency': 'USD', 'timestamp': NOW.isoformat()}]*2}
    await monitor.poll(read)
    rows = store.repo.all(OBSERVATIONS)
    assert len(rows) == 6 and all(row['quality_status'] == 'UNAVAILABLE' for row in rows)
    assert all(row['event_at'] is None for row in rows)
    assert not hasattr(monitor, 'submit_order')


@pytest.mark.parametrize('code', ['http_404', 'http_429', 'timeout', 'response_unavailable'])
async def test_read_failures_stay_separate_from_orders(store, code):
    monitor = MarketReferenceMonitor(store.repo, clock=lambda: NOW)
    async def read(*args): return {'data': None, 'error': code}
    result = await monitor.poll(read)
    assert all(row['quality_status'] == 'UNAVAILABLE' for row in store.repo.all(OBSERVATIONS))
    assert result['items']['IEF']['latency_class'] == 'UNKNOWN'
    assert store.repo.all('orders') == []


def test_permissions_secrets_are_not_stored_and_cannot_verify():
    license = proof(); license['evidence_url'] = 'https://fixture.invalid?api_key=DO_NOT_SAVE'
    row = candle(proof=license)
    assert 'DO_NOT_SAVE' not in str(row) and row['latency_class'] == 'UNKNOWN'


def test_schema_upgrade_preserves_existing_order_ids(tmp_path):
    import sqlite3
    path = tmp_path/'old.db'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE orders (seq INTEGER PRIMARY KEY, record_id TEXT UNIQUE, payload TEXT NOT NULL)')
    db.execute("INSERT INTO orders VALUES(7,'unchanged','{\"id\":\"unchanged\",\"qty\":\"0.123\"}')")
    db.execute('PRAGMA user_version=2'); db.commit(); db.close()
    repo = Repository(path, backend='sqlite')
    try:
        assert repo.db.execute('PRAGMA user_version').fetchone()[0] == 6
        for kind in ('news_ingestions', 'news_enrichments', 'macro_context_snapshots','llm_calls','llm_audit_bodies','macro_context_analysis'):
            assert repo.all(kind) == []
        assert repo.all('research_events') == []
        assert repo.db.execute('SELECT seq, record_id FROM orders').fetchone() == (7, 'unchanged')
        assert repo.all('orders')[0]['qty'] == '0.123'
        assert repo.all('fred_reference_observations') == []
    finally: repo.close()


def test_reference_panel_handles_empty_unknown_and_missing_dates(store):
    from sessionquant.dashboard.reference_panel import reference_rows
    rows = reference_rows(store.publish(NOW))
    assert len(rows) == 6 and all(r['품질'] == 'UNKNOWN' and r['원본 시각'] == '—' for r in rows)


def test_reference_panel_does_not_show_expired_quality_as_current_success(store):
    from sessionquant.dashboard.reference_panel import reference_rows
    series(store)
    saved = store.publish(NOW)
    assert next(row for row in reference_rows(saved, NOW) if row['종목'] == 'IEF')['품질'] == 'GOOD'
    assert next(row for row in reference_rows(saved, NOW+timedelta(seconds=301)) if row['종목'] == 'IEF')['품질'] == 'STALE'


@pytest.mark.parametrize('value', [{'enabled': 'false'}, {'price_seconds': 1}, {'candle_seconds': 301},
    {'calls_per_cycle': True}, {'unknown': True}])
def test_reference_config_rejects_invalid_intervals(value):
    from sessionquant.session_config import SessionConfig
    with pytest.raises(ValueError): SessionConfig(market_reference=value)


async def test_managed_source_delivers_held_data_before_references_and_shrinks_budget(tmp_path):
    from sessionquant.market.source import TossSessionSource
    from test_watchlist_source import Market
    from watchlist_fixtures import context, observe, NOW as CAP_NOW
    repo, _, _, _, manager = context(tmp_path)
    at = CAP_NOW+timedelta(seconds=60)
    try:
        await manager.refresh(CAP_NOW); observe(manager)
        for budget in (8, 24):
            events = []
            class Client(Market):
                async def stocks(self, symbols):
                    if symbols == list(REFERENCES): events.append('references')
                    return await super().stocks(symbols)
                async def orderbook(self, symbol): return {}
                async def reference_candles(self, symbol, **kwargs): return {'candles': []}
            def priority(batch):
                if 'T001' in {q.symbol for q in batch.quotes}: events.append('held_delivered')
            client = Client(lambda: at)
            monitor = MarketReferenceMonitor(repo, clock=lambda: at)
            source = TossSessionSource(client, clock=lambda: at, watchlist=manager,
                max_api_calls=budget, reference_monitor=monitor, on_priority_batch=priority)
            await source.poll([], priority_symbols=['T001'])
            assert 'held_delivered' in events and len(client.calls) <= budget
            if 'references' in events: assert events.index('held_delivered') < events.index('references')
            if budget == 8: assert 'references' not in events
            assert repo.all('orders') == []
            assert not any(s in manager.light_symbols(at) for s in REFERENCES)
    finally: repo.close()
