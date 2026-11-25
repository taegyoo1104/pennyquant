"""기존 장부 ID와 별개인 불변 수신·처리 원장을 기록한다."""
from datetime import datetime, timezone
from dataclasses import asdict
from .storage import StoragePolicy, due, store
from uuid import uuid4
import re
from urllib.parse import urlsplit, urlunsplit

from ..models import utc
from ..portfolio.models import serialize
from ..market_reference.contracts import identity

CONTROL = 'research_recording_v1'
KINDS = {'news_events', 'news_analysis', 'macro_news', 'macro_analysis', 'economic_events',
         'llm_usage', 'universe_history', 'market_reference_observations', 'fred_reference_observations','news_ingestions','news_enrichments','macro_context_snapshots'}
CONTROLS = {'market_reference_view_v1', 'watchlist_snapshots_v1', 'watchlist_history_selection_v1'}


def clock():
    return datetime.now(timezone.utc)


def clean(value):
    """계좌·인증 키는 내보내지 않고 URL의 인증·쿼리·fragment를 제거한다."""
    if isinstance(value, dict):
        return {key: ('scope:'+identity(item)[:24] if key == 'account_scope' and item not in {None,'UNKNOWN'} else clean(item)) for key, item in value.items() if not re.search(
            r'(?i)(secret|password|authorization|api.?key|access.?token|client.?id|account.?seq|account.?number)', key)}
    if isinstance(value, list): return [clean(item) for item in value]
    if isinstance(value, str):
        if re.search(r'(?i)(bearer\s+\S+|(?:api.?key|secret|password|token)\s*[:=]\s*\S+)', value): return '[인증정보 제거]'
        if value.startswith(('https://', 'http://')):
            parsed = urlsplit(value)
            return urlunsplit((parsed.scheme, parsed.hostname or '', parsed.path, '', ''))
    return value


def time_or_none(value):
    try: return utc(value).isoformat() if value else None
    except (ValueError, TypeError, AttributeError): return None


def append(repo, kind, data, *, available_at=None):
    state = repo.get_control(CONTROL, {}) or {}
    if state.get('active') is not True: return None
    policy = StoragePolicy(**state.get('auxiliary_storage',{}))
    if policy.bounded and kind not in {'recording_start','recording_stop','engine_batch'}:
        return None  # Primary tables/controls already retain the operational data.
    ingested = clock()
    payload = clean(serialize(data))
    available = utc(available_at or ingested)
    if isinstance(payload, dict):
        times = [time_or_none(payload.get(key)) for key in ('received_at', 'ingested_at', 'analyzed_at', 'known_at', 'available_at')]
        available = max([available, *(utc(item) for item in times if item)])
    event = {'schema_version': 1, 'kind': kind, 'run_id': state['run_id'],
        'source': state.get('source', 'UNKNOWN'), 'synthetic': state.get('synthetic') is True,
        'event_at': time_or_none(payload.get('event_at') or payload.get('timestamp')) if isinstance(payload, dict) else None,
        'published_at': time_or_none(payload.get('published_at')) if isinstance(payload, dict) else None,
        'received_at': time_or_none(payload.get('received_at')) if isinstance(payload, dict) else None,
        'ingested_at': ingested.isoformat(), 'available_at': available.isoformat(), 'data': payload}
    event['id'] = 'research:'+identity({key: value for key, value in event.items() if key != 'ingested_at'})
    if policy.bounded:
        saved = store(repo,policy,'research_events',event,kind,ingested,
                      urgent=kind in {'recording_start','recording_stop'} or bool(data.get('execution_checks') or data.get('sell_execution_checks')))
        return event if saved else None
    if repo.get_record('research_events', event['id']) is None: repo.record('research_events', event)
    return event


def capture_record(repo, kind, data):
    if kind in KINDS or (kind == 'pipeline_status' and data.get('kind') == 'watchlist_focus'):
        append(repo, kind, data)


def capture_control(repo, name, data):
    if name in CONTROLS: append(repo, name, data)


def start(repo, engine, *, source):
    """매 실행 기록 경계를 만들되 기존 주문·이벤트를 수정하지 않는다."""
    state = {'active': True, 'run_id': uuid4().hex, 'source': source, 'synthetic': engine.synthetic, 'auxiliary_storage':asdict(engine.auxiliary_storage)}
    repo.set_control(CONTROL, state)
    append(repo, 'recording_start', {'strict_market_data': engine.market.strict_data,
        'paper_data_policy':engine.market.paper_data_policy,
        'initial_flat': not engine.broker.positions and not engine.broker.get_open_orders(),
        'settings': serialize(engine.broker.settings), 'strategy': engine.strategy,
        'exit_policy': serialize(engine.exit_policy),
        'candidate_selection':serialize(engine.candidate_selector.settings),
        'source_reference_filter':serialize(engine.macro_reference_risk.settings) if engine.macro_reference_risk else None,
        'execution_guard_version':1 if engine.execution_guard.enabled else None,
        'quote_recovery_wait':engine.quote_recovery_wait,
        'submission_quote_buys':engine.submission_quote_buys,
        'submission_quote_sells':engine.submission_quote_sells,
        'sell_execution_guard_version':1 if engine.submission_quote_sells and engine.sell_execution_guard.enabled else None,
        'recording_version': 1, 'auxiliary_storage':asdict(engine.auxiliary_storage)})
    for name in CONTROLS:
        value = repo.get_control(name)
        if value is not None:append(repo,name,value)


def stop(repo):
    state = repo.get_control(CONTROL, {}) or {}
    if state.get('active'):
        append(repo, 'recording_stop', {})
        repo.set_control(CONTROL, {**state, 'active': False})


def frame(engine, events, now, *, manual_halt):
    """가격 수신 시각과 실제 전략 평가 시각을 분리하고 빈 청산 시계도 기록한다."""
    from ..session_engine import event_time
    policy = engine.auxiliary_storage
    urgent = bool(engine.execution_guard.checks or engine.sell_execution_guard.checks)
    if not due(engine.repo,policy,'engine_batch',clock(),urgent=urgent):
        return
    normalized = [{**raw,'timing':{
        'event_at':time_or_none(raw['data'].get('timestamp') or raw['data'].get('published_at')),
        'published_at':time_or_none(raw['data'].get('published_at')),
        'received_at':utc(raw['data']['checked_at'] if raw['kind']=='eligibility' else raw['data']['received_at'])
            if 'restored_at' in raw or 'delivered_at' in raw else event_time(raw),
        'available_at':now,'source':raw['data'].get('source','UNKNOWN')}} for raw in events]
    payload = {'decision_at': now, 'events': normalized,
        'candidate_scope_symbols':engine._batch_candidate_scope,'portfolio_selection':engine.selection_frame,
        'news_contexts': engine._batch_news_contexts, 'scope_blockers': engine._batch_scope_blockers,
        'decisions': engine._batch_decisions,
        'quote_entry_allowed':engine._batch_quote_entry_allowed if engine.quote_entry_guard is not None else None,
        'execution_checks':engine.execution_guard.checks if engine.execution_guard.enabled else None,
        'execution_guard_version':1 if engine.execution_guard.enabled else None,
        'quote_recovery_wait':engine.quote_recovery_wait,
        'submission_quote_buys':engine.submission_quote_buys,
        'submission_quote_sells':engine.submission_quote_sells,
        **({'sell_execution_guard_version':1,'sell_execution_checks':engine.sell_execution_guard.checks,
            'sell_execution_check_overflow':engine.sell_execution_guard.overflow_count}
           if engine.submission_quote_sells and engine.sell_execution_guard.enabled else {}),
        'execution_check_overflow':engine.execution_guard.overflow_count,
        **({'quote_wait_evidence':engine._batch_quote_wait_evidence} if engine._batch_quote_wait_evidence else {}),
        'quality_rows': [{key:row.get(key) for key in ('symbol','eligibility_verified','quote_fresh','rvol','bars',
                         'strategy_bars','strategy_rvol','strategy_rvol_status','collection_warnings')}
                         for symbol,row in engine.market.quality_rows.items() if symbol in {item['symbol'] for item in engine._batch_decisions}],
        'manual_halt': manual_halt, 'control_state': {key: engine.control_state[key]
            for key in ('running', 'draining') if key in (engine.control_state or {})},
        'operator_exits': {symbol: item['reason'] for symbol, item in engine.broker.exit_intents.items()
            if item['reason'] in {'manual_market_sell', 'operator_stop_all'}}}
    if policy.bounded:
        # Full selected rows already exist in the latest view and order-linked
        # archive. Keep only identity/expiry here, not another large copy.
        selection = payload['portfolio_selection']
        if selection:
            payload['portfolio_selection'] = {'id':selection['id'], 'rows':[
                {key:row.get(key) for key in ('symbol','scope_valid_until')} for row in selection.get('rows',[])]}
        payload['decisions'] = [{key:row.get(key) for key in ('symbol','strategy','eligible','blockers','score')}
                                for row in payload['decisions']]
        payload['quality_rows'] = [{key:row.get(key) for key in ('symbol','eligibility_verified','quote_fresh','strategy_rvol_status')}
                                   for row in payload['quality_rows']]
        payload['recording_mode'] = policy.mode
    append(engine.repo, 'engine_batch', payload, available_at=now)
